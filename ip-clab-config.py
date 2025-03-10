#!/usr/bin/env python3
import yaml
import subprocess
import time
import os

def parse_input_file(filename):
    # Input dosyasını oku
    with open(filename, 'r') as file:
        lines = file.readlines()
    # Lab ismini al
    lab_name = lines[0].split(': ')[1].strip()
    # Bağlantıları parse et
    connections = []
    for line in lines[1:]:
        parts = line.strip().split()
        if len(parts) == 4:
            connections.append({
                'device1': parts[0],
                'interface1': parts[1][1:],
                'device2': parts[2],
                'interface2': parts[3][1:]
            })
    return lab_name, connections

def create_yaml_structure(lab_name, connections):
    # Tüm benzersiz cihazları bul
    devices = set()
    for conn in connections:
        devices.add(conn['device1'])
        devices.add(conn['device2'])
    # YAML yapısını oluştur
    yaml_dict = {
        'name': lab_name,
        'topology': {
            'nodes': {},
            'links': []
        }
    }
    # Cihazları ekle
    for device in sorted(devices, key=lambda x: (x[0], int(x[1:]))): # Önce r/s'ye göre, sonra numaraya göre sırala
        device_num = int(device[1:])
        # Temel cihaz özellikleri
        device_config = {
            'kind': 'cisco_iol',
            'image': 'vrnetlab/cisco_iol:17.12.01'
        }
        # Switch mi router mı kontrolü
        if device.startswith('s'):
            device_config['type'] = 'l2'
            device_config['mgmt-ipv4'] = f'172.20.20.{100 + device_num}'
        else:  # router
            device_config['mgmt-ipv4'] = f'172.20.20.{10 + device_num}'
        yaml_dict['topology']['nodes'][device] = device_config
    # Bağlantıları ekle
    for conn in connections:
        endpoint1 = f"{conn['device1']}:Ethernet{conn['interface1']}"
        endpoint2 = f"{conn['device2']}:Ethernet{conn['interface2']}"
        yaml_dict['topology']['links'].append({
            'endpoints': [endpoint1, endpoint2]
        })
    return yaml_dict

def write_yaml_file(yaml_dict, output_filename):
    # YAML dosyasını oluştur
    with open(output_filename, 'w') as file:
        yaml.dump(yaml_dict, file, default_flow_style=False, sort_keys=False)

def enrich_inventory(inventory_path):
    # Mevcut inventory'yi oku
    with open(inventory_path, 'r') as file:
        inventory = yaml.safe_load(file)

    # Cisco vars ekle
    if 'all' not in inventory:
        inventory['all'] = {}
    
    if 'children' not in inventory['all']:
        inventory['all']['children'] = {}
    
    if 'cisco_iol' not in inventory['all']['children']:
        inventory['all']['children']['cisco_iol'] = {}

    if 'vars' not in inventory['all']['children']['cisco_iol']:
        inventory['all']['children']['cisco_iol']['vars'] = {}

    # Gerekli parametreleri ekle
    inventory['all']['children']['cisco_iol']['vars'].update({
        'ansible_user': 'admin',
        'ansible_password': 'admin',
        'ansible_network_os': 'ios',
        'ansible_connection': 'network_cli',
        'ansible_become': True,
        'ansible_become_method': 'enable',
        'ansible_become_password': 'admin'
    })

    # Global vars ekle
    if 'vars' not in inventory['all']:
        inventory['all']['vars'] = {}
    
    inventory['all']['vars']['ansible_httpapi_use_proxy'] = False

    # Güncellenmiş inventory'yi yaz
    with open(inventory_path, 'w') as file:
        yaml.dump(inventory, file, default_flow_style=False)

def create_loopback_playbook():
    playbook = """---
- name: Configure Loopback Interfaces
  hosts: cisco_iol
  gather_facts: false
  connection: network_cli
  tasks:
    - name: Get device number
      set_fact:
        device_number: "{{ inventory_hostname.split('-')[-1][1:] }}"
        device_type: "{{ inventory_hostname.split('-')[-1][0] }}"
      no_log: true

    - name: Configure Loopbacks and IPs
      block:
        - name: Configure Router Loopbacks
          cisco.ios.ios_config:
            lines:
              - interface Loopback0
              - ip address 1.1.{{ device_number }}.1 255.255.255.255
              - no shutdown
              - interface Loopback10
              - ip address 172.16.{{ device_number }}.1 255.255.255.0
              - no shutdown
          when: device_type == 'r'
          register: router_config

        - name: Configure Switch Loopbacks
          cisco.ios.ios_config:
            lines:
              - interface Loopback0
              - ip address 2.2.{{ device_number }}.1 255.255.255.255
              - no shutdown
              - interface Loopback10
              - ip address 172.17.{{ device_number }}.1 255.255.255.0
              - no shutdown
          when: device_type == 's'
          register: switch_config

        - name: Display Configuration Summary
          debug:
            msg: "{{ inventory_hostname.split('-')[-1] }} Loopback Yapılandırması:
                  \\n- Loopback0: {{ '1.1.' if device_type == 'r' else '2.2.' }}{{ device_number }}.1/32
                  \\n- Loopback10: {{ '172.16.' if device_type == 'r' else '172.17.' }}{{ device_number }}.1/24"
          when: router_config.changed or switch_config.changed
"""
    with open('loopback.yaml', 'w') as file:
        file.write(playbook)

def create_interface_ip_playbook(connections, lab_name):
    class IPTracker:
        def __init__(self):
            self.used_subnets = set()
            self.switch_subnets = {}

			
        def get_ip_pair(self, r1, r2):
            # Router-Router bağlantıları için /30
            r1_num = int(r1[1:])
            r2_num = int(r2[1:])
            subnet = f"10.{r1_num}.{r2_num}.0"
            if subnet not in self.used_subnets:
                self.used_subnets.add(subnet)
                return f"10.{r1_num}.{r2_num}.1", f"10.{r1_num}.{r2_num}.2"
            return None, None

        def get_switch_subnet_ip(self, switch, router, interface):
            # Switch ID'sini al
            switch_id = int(switch[1:])
            
            if switch not in self.switch_subnets:
                # Switch ID'sine göre subnet bloğu oluştur
                self.switch_subnets[switch] = {
                    'subnet': f"192.168.{switch_id}.0/28",
                    'next_host': 1
                }
											  

            # Bir sonraki kullanılabilir IP'yi al
            next_ip = self.switch_subnets[switch]['next_host']
            self.switch_subnets[switch]['next_host'] += 1
            
            # 192.168.switch_id.1, 192.168.switch_id.2, ... şeklinde IP'ler ata
            return f"192.168.{switch_id}.{next_ip}/28"

    ip_tracker = IPTracker()
    interface_configs = []

    # Önce switch bağlantılarını işle
    for conn in connections:
        if conn['device1'].startswith('s') or conn['device2'].startswith('s'):
            switch = conn['device1'] if conn['device1'].startswith('s') else conn['device2']
            router = conn['device2'] if conn['device1'].startswith('s') else conn['device1']
            router_interface = conn['interface2'] if conn['device1'].startswith('s') else conn['interface1']
            
            ip = ip_tracker.get_switch_subnet_ip(switch, router, router_interface)
            interface_configs.append({
                'device': router,
                'interface': router_interface,
                'ip': ip,
                'switch': switch
            })

    # Router-Router bağlantılarını işle
    for conn in connections:
        if conn['device1'].startswith('r') and conn['device2'].startswith('r'):
            ip1, ip2 = ip_tracker.get_ip_pair(conn['device1'], conn['device2'])
            if ip1 and ip2:
                interface_configs.append({
                    'device1': conn['device1'],
                    'interface1': conn['interface1'],
                    'ip1': ip1,
                    'device2': conn['device2'],
                    'interface2': conn['interface2'],
                    'ip2': ip2
                })

    # Playbook template
    playbook = """---
- name: Configure Interface IPs and Status
  hosts: cisco_iol
  gather_facts: false
  connection: network_cli

  tasks:
    - name: Check if device is router
      set_fact:
        is_router: "{{ true if inventory_hostname.split('-')[-1].startswith('r') else false }}"
      no_log: true

    - name: Enable all interfaces on switches
      cisco.ios.ios_interfaces:
        config:
          - name: "{{ item }}"
            enabled: true
        state: merged
      loop:
        - Ethernet0/0
        - Ethernet0/1
        - Ethernet0/2
        - Ethernet0/3
        - Ethernet1/0
        - Ethernet1/1
        - Ethernet1/2
        - Ethernet1/3
      when: not is_router
"""

    # Switch bağlantıları için task'ları oluştur
    for config in [c for c in interface_configs if 'switch' in c]:
        playbook += f"""    - name: Configure {config['device']} interface to {config['switch']}
      block:
        - name: Enable interface
          cisco.ios.ios_interfaces:
            config:
              - name: Ethernet{config['interface']}
                enabled: true
            state: merged
          when: inventory_hostname == 'clab-{lab_name}-{config['device']}'

        - name: Configure IP
          cisco.ios.ios_l3_interfaces:
            config:
              - name: Ethernet{config['interface']}
                ipv4:
                  - address: {config['ip']}
          when: inventory_hostname == 'clab-{lab_name}-{config['device']}'
      when: inventory_hostname == 'clab-{lab_name}-{config['device']}'
"""

    # Router-Router bağlantıları için task'ları oluştur
    for config in [c for c in interface_configs if 'device1' in c]:
        playbook += f"""    - name: Configure {config['device1']}-{config['device2']} link ({config['ip1']}/30 - {config['ip2']}/30)
      block:
        - name: Enable {config['device1']} {config['interface1']}
          cisco.ios.ios_interfaces:
            config:
              - name: Ethernet{config['interface1']}
                enabled: true
            state: merged
          when: inventory_hostname == 'clab-{lab_name}-{config['device1']}'

        - name: Configure {config['device1']} {config['interface1']} IP
          cisco.ios.ios_l3_interfaces:
            config:
              - name: Ethernet{config['interface1']}
                ipv4:
                  - address: {config['ip1']}/30
          when: inventory_hostname == 'clab-{lab_name}-{config['device1']}'

        - name: Enable {config['device2']} {config['interface2']}
          cisco.ios.ios_interfaces:
            config:
              - name: Ethernet{config['interface2']}
                enabled: true
            state: merged
          when: inventory_hostname == 'clab-{lab_name}-{config['device2']}'

        - name: Configure {config['device2']} {config['interface2']} IP
          cisco.ios.ios_l3_interfaces:
            config:
              - name: Ethernet{config['interface2']}
                ipv4:
                  - address: {config['ip2']}/30
          when: inventory_hostname == 'clab-{lab_name}-{config['device2']}'
      when: inventory_hostname in ['clab-{lab_name}-{config['device1']}', 'clab-{lab_name}-{config['device2']}']
"""

    # Sonuç gösterme task'ı
    playbook += """    - name: Show IP configuration
      ios_command:
        commands:
          - show ip interface brief
      register: ip_status
      when: is_router

    - name: Display IP configuration
      debug:
        msg: "{{ ip_status.stdout_lines }}"
      when: is_router

    - name: Show switch interface status
      ios_command:
        commands:
          - show interfaces status
      register: sw_status
      when: not is_router

    - name: Display switch interface status
      debug:
        msg: "{{ sw_status.stdout_lines }}"
      when: not is_router
"""

    # Playbook'u kaydet
    with open('interface_ip.yaml', 'w') as file:
        file.write(playbook)

    # Yapılandırma özetini göster
    print("\nYapılandırılacak interface IP'leri:")
    
    # Switch gruplarını göster
    switch_groups = {}
    for config in [c for c in interface_configs if 'switch' in c]:
        if config['switch'] not in switch_groups:
            switch_groups[config['switch']] = []
        switch_groups[config['switch']].append(config)

    print("\nSwitch grupları:")
    for switch, configs in switch_groups.items():
        switch_id = int(switch[1:])
        print(f"\n{switch} grubu (192.168.{switch_id}.0/28):")
        for config in configs:
            print(f"  {config['device']}({config['interface']}): {config['ip']}")
    
    print("\nRouter-Router bağlantıları:")
    for config in [c for c in interface_configs if 'device1' in c]:
        print(f"{config['device1']}({config['interface1']}) <-> {config['device2']}({config['interface2']}): "
              f"{config['ip1']}/30 - {config['ip2']}/30")

    return interface_configs

def save_startup_config(lab_name, inventory_path):
    save_playbook = """---
- name: Save Running Config to Startup Config
  hosts: cisco_iol
  gather_facts: false
  connection: network_cli

  tasks:
    - name: Save configuration
      cli_command:
        command: copy running-config startup-config
        prompt: 'Destination filename \[startup-config\]'
        answer: "\r"
      register: output
      ignore_errors: yes

    - name: Display save output
      debug:
        var: output.stdout_lines
      when: output is defined"""
    
    # Save playbook'u kaydet
    with open('save_config.yaml', 'w') as file:
        file.write(save_playbook)

    # Save playbook'u çalıştır
    print("\nKonfigürasyonlar kaydediliyor...")
    result = subprocess.run(
        ['ansible-playbook', '-i', inventory_path, 'save_config.yaml'],
        capture_output=True, 
        text=True,
        env=dict(os.environ, ANSIBLE_DISPLAY_SKIPPED_HOSTS='false')
    )

    if result.returncode == 0:
        print("Konfigürasyonlar başarıyla kaydedildi")
        # Başarılı kayıt mesajlarını göster
        for line in result.stdout.split('\n'):
            if 'bytes copied' in line:
                print(line.strip())
    else:
        print("Konfigürasyon kaydetme sırasında hata oluştu:")
        if result.stderr:
            print(result.stderr)


def deploy_lab(yaml_file, lab_name, connections):
    try:
        # Containerlab deploy komutunu çalıştır
        subprocess.run(['containerlab', 'deploy', '-t', yaml_file], check=True)
        print(f"Lab başarıyla deploy edildi: {lab_name}")
        
        # Inventory dosyasının oluşmasını bekle
        inventory_path = f"clab-{lab_name}/ansible-inventory.yml"
        max_attempts = 30
        attempt = 0
        while not os.path.exists(inventory_path) and attempt < max_attempts:
            time.sleep(1)
            attempt += 1

        if os.path.exists(inventory_path):
            # Inventory dosyasını zenginleştir
            print(f"Inventory dosyası bulundu: {inventory_path}")
            time.sleep(2)  # Dosyanın tamamen yazılmasını bekle
            enrich_inventory(inventory_path)
            print("Inventory dosyası zenginleştirildi")

            # Loopback playbook'u oluştur
            create_loopback_playbook()
            print("Loopback playbook oluşturuldu")

            # Cihazların hazır olmasını bekle
            print("Cihazların hazır olması bekleniyor...")
            max_attempts = 20  # Deneme sayısını artırdık
            for attempt in range(max_attempts):
                try:
                    # SSH bağlantısını test et
                    result = subprocess.run(
                        ['ansible', 'cisco_iol', '-i', inventory_path, '-m', 'ping'],
                        capture_output=True,
                        text=True
                    )
                    if result.returncode == 0:
                        print("Cihazlar hazır!")
                        # Biraz daha bekle
                        print("Cihazların tam olarak hazır olması için 30 saniye bekleniyor...")
                        time.sleep(30)
                        break
                    print(f"Deneme {attempt + 1}/{max_attempts}... Cihazlar henüz hazır değil.")
                    time.sleep(10)
                except:
                    time.sleep(10)

            # Loopback yapılandırmasını uygula
            # Loopback yapılandırmasını uygula
            print("\nLoopback yapılandırması uygulanıyor...")
            print("-" * 50)
            result = subprocess.run(
                ['ansible-playbook', '-i', inventory_path, 'loopback.yaml'],
                capture_output=True,
                text=True,
                env=dict(os.environ, ANSIBLE_DISPLAY_SKIPPED_HOSTS='false')
            )

            if result.returncode == 0:
                # Sadece yapılandırma özetini göster
                for line in result.stdout.split('\n'):
                    if "Loopback Yapılandırması" in line:
                        print(line.replace('\\n', '\n').replace('msg:', '').strip())
                print("-" * 50)
                print("Loopback yapılandırması başarıyla tamamlandı")
            else:
                print("Loopback yapılandırması sırasında hata oluştu:")
                print(result.stderr)

            #  Interface IP'lerini yapılandır
            print("\nInterface IP'leri yapılandırılıyor...")
            interface_configs = create_interface_ip_playbook(connections, lab_name)

            result = subprocess.run(
                ['ansible-playbook', '-i', inventory_path, 'interface_ip.yaml'],
                capture_output=True,
                text=True,
                env=dict(os.environ, ANSIBLE_DISPLAY_SKIPPED_HOSTS='false')
            )

            # Interface IP çıktılarını işle
            if result.returncode == 0:
                print("\nInterface IP yapılandırması başarıyla tamamlandı")
                
                # Konfigürasyonu kaydet
                print("\nKonfigürasyonlar kaydediliyor...")
                save_startup_config(lab_name, inventory_path)
            else:
                print("\nInterface IP yapılandırması sırasında hata oluştu:")
                print(result.stderr)
        else:
            print("Inventory dosyası bulunamadı")

    except subprocess.CalledProcessError as e:
        print(f"Lab deploy edilirken hata oluştu: {e}")
    except Exception as e:
        print(f"Beklenmeyen bir hata oluştu: {e}")

def create_ansible_cfg():
    config = """[defaults]
connection = paramiko
host_key_checking = False
"""
    with open('ansible.cfg', 'w') as file:
        file.write(config)

def main():
    # Ansible config dosyasını oluştur
    create_ansible_cfg()
   
    input_filename = 'input.txt'
    # Input dosyasını parse et
    lab_name, connections = parse_input_file(input_filename)
    # Output dosya adını lab isminden oluştur
    output_filename = f"{lab_name}.yaml"
    # YAML yapısını oluştur
    yaml_dict = create_yaml_structure(lab_name, connections)
    # YAML dosyasını yaz
    write_yaml_file(yaml_dict, output_filename)
    print(f"YAML dosyası başarıyla oluşturuldu: {output_filename}")
    
    # Lab'ı deploy et ve inventory'yi zenginleştir
    deploy_lab(output_filename, lab_name, connections)

if __name__ == "__main__":
    main()
