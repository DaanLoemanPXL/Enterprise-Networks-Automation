#!/usr/bin/env python3
"""
Task 36 – NETCONF Deployment Script
Router: LAB-RA07-C02-R01
Single Source of Truth: GitHub (iosxe_router_config.xml)
Datastore: candidate → commit naar running
Auteur: Daan Loeman
"""

import sys
import requests
from ncclient import manager
from ncclient.transport.errors import SSHError, AuthenticationError
from ncclient.operations.errors import TimeoutExpiredError
import traceback

# ─── INSTELLINGEN – pas deze aan naar jouw lab ─────────────────────────────
GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/DaanLoemanPXL/Enterprise-Networks-Automation/main/iosxe_router_config.xml"
)

DEVICE = {
    "host":           "172.17.7.65",   
    "port":           830,            
    "username":       "admin",
    "password":       "cisco123",
    "hostkey_verify": False,           
    "device_params":  {"name": "iosxe"},
    "timeout":        60,
}
# ───────────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════
# STAP 1 – Configuratie ophalen uit GitHub
# ═══════════════════════════════════════════════════════════════

def fetch_config_from_github(url: str) -> str:
    """
    Haal de YANG XML-configuratie op vanuit GitHub.
    GitHub is de single source of truth voor alle netwerkconfig.
    """
    print(f"\n[STAP 1] Ophalen configuratie van GitHub...")
    print(f"         URL: {url}")

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        config_text = response.text

        print(f"[+] Configuratie opgehaald ({len(config_text)} bytes)")
        return config_text

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else "onbekend"
        print(f"[-] HTTP fout {status} bij ophalen van GitHub.")
        if status == 404:
            print("    Bestand niet gevonden – controleer de repository URL en bestandsnaam.")
        sys.exit(1)

    except requests.exceptions.ConnectionError:
        print("[-] Kan GitHub niet bereiken.")
        print("    Controleer: internetverbinding, DNS, firewall.")
        sys.exit(1)

    except requests.exceptions.Timeout:
        print("[-] Timeout bij ophalen van GitHub (>15 seconden).")
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════
# STAP 2 – Deploy via NETCONF (candidate → commit)
# ═══════════════════════════════════════════════════════════════

def deploy_config(config_xml: str) -> bool:
    """
    Verbind met het IOS-XE toestel via NETCONF.
    Volgorde: lock → edit-config → validate → commit → unlock
    Bij fout: discard-changes (atomair – niets wordt half toegepast)
    """
    print(f"\n[STAP 2] Verbinden met router {DEVICE['host']}:{DEVICE['port']}...")

    try:
        with manager.connect(**DEVICE) as conn:
            print(f"[+] NETCONF sessie actief (session-id: {conn.session_id})")

            # ── 2a. Vergrendel candidate datastore ──────────────────────────
            print("\n[2a] Vergrendelen candidate datastore...")
            conn.lock(target="candidate")
            print("[+] Candidate vergrendeld – andere sessies kunnen nu niet schrijven")

            try:
                # ── 2b. edit-config naar candidate ─────────────────────────
                print("\n[2b] Uitvoeren edit-config naar candidate datastore...")
                conn.edit_config(target="candidate", config=config_xml)
                print("[+] edit-config succesvol – configuratie staat nu in candidate")

                # ── 2c. Valideer candidate (bonus: expliciete validate stap) ─
                print("\n[2c] Valideren van candidate configuratie...")
                conn.validate(source="candidate")
                print("[+] Validatie geslaagd – geen syntaxfouten gevonden")

                # ── 2d. Commit naar running ─────────────────────────────────
                print("\n[2d] Committen naar running datastore...")
                conn.commit()
                print("[+] Commit geslaagd – configuratie is nu ACTIEF op het toestel!")
                return True

            except Exception as deploy_error:
                # ── FOUTAFHANDELING: discard-changes ───────────────────────
                print(f"\n[!] Fout tijdens deployment:")
                print(f"    Type : {type(deploy_error).__name__}")
                (f"    Detail: {str(deploy_error)}")
                # Print full traceback for line number
                traceback.print_exc()
                try:
                    conn.discard_changes()
                    print("[+] discard-changes OK – candidate is teruggeplaatst naar running state")
                    print("    Geen wijzigingen zijn doorgevoerd op het toestel (atomair gedrag).")
                except Exception as discard_error:
                    print(f"[-] discard-changes zelf mislukt: {discard_error}")
                return False

            finally:
                # Altijd ontgrendelen, ook bij fout
                print("\n[2e] Ontgrendelen candidate datastore...")
                try:
                    conn.unlock(target="candidate")
                    print("[+] Candidate datastore ontgrendeld")
                except Exception as unlock_error:
                    print(f"[!] Ontgrendelen mislukt (sessie al gesloten?): {unlock_error}")

    except AuthenticationError:
        print("[-] Authenticatiefout – controleer username/password in script")
        print(f"    Gebruiker: {DEVICE['username']}")
        return False

    except SSHError as e:
        print(f"[-] SSH/NETCONF verbindingsfout: {e}")
        print(f"    Controleer: IP {DEVICE['host']}, poort {DEVICE['port']}, NETCONF actief?")
        print("    Op het toestel: 'show platform software yang-management process'")
        return False

    except TimeoutExpiredError:
        print(f"[-] NETCONF time-out (>{DEVICE['timeout']}s) – toestel reageert niet op SSH/830")
        return False

    except Exception as e:
        print(f"[-] Onverwachte fout: {type(e).__name__}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# STAP 3 – Verificatie (bonus: get-config teruglezen)
# ═══════════════════════════════════════════════════════════════

def verify_deployment() -> None:
    """
    Lees hostname en Gi0/0/0.75 IP terug uit running configuratie
    om te bevestigen dat de deployment correct is doorgevoerd.
    """
    print(f"\n[STAP 3] Verificatie – running configuratie teruglezen...")

    filter_xml = """
    <filter type="subtree">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <hostname/>
        <interface>
          <GigabitEthernet>
            <name>0/0/0.75</name>
          </GigabitEthernet>
        </interface>
      </native>
    </filter>
    """

    try:
        with manager.connect(**DEVICE) as conn:
            result = conn.get_config(source="running", filter=filter_xml)
            result_str = str(result)

            # Check hostname
            hostname_ok = "LAB-RA07-C02-R01" in result_str
            # Check management IP
            mgmt_ip_ok = "172.17.7.65" in result_str

            print(f"  Hostname 'LAB-RA07-C02-R01' : {'[✓] aanwezig' if hostname_ok else '[✗] NIET gevonden'}")
            print(f"  IP 172.17.7.65 (Gi0/0/0.75) : {'[✓] aanwezig' if mgmt_ip_ok else '[✗] NIET gevonden'}")

            if hostname_ok and mgmt_ip_ok:
                print("[+] Verificatie geslaagd – deployment bevestigd in running config")
            else:
                print("[!] Verificatie gedeeltelijk mislukt – controleer de running config handmatig")
                print("    Op het toestel: 'show running-config | section interface'")

    except Exception as e:
        print(f"[-] Verificatie mislukt: {e}")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 62)
    print("  NETCONF Deployment – Task 36")
    print("  Router : LAB-RA07-C02-R01")
    print(f"  Target : {DEVICE['host']}:{DEVICE['port']}")
    print("  SSOT   : GitHub")
    print("=" * 62)

    # Stap 1: Config ophalen uit GitHub
    config_xml = fetch_config_from_github(GITHUB_RAW_URL)

    # Stap 2: Deployen via NETCONF naar candidate, commit naar running
    success = deploy_config(config_xml)

    if success:
        # Stap 3: Verificatie via get-config
        verify_deployment()
        print("\n" + "=" * 62)
        print("  [✓] DEPLOYMENT VOLTOOID")
        print("  Configuratie is actief, atomair toegepast en geverifieerd.")
        print("=" * 62)
    else:
        print("\n" + "=" * 62)
        print("  [✗] DEPLOYMENT MISLUKT")
        print("  Geen wijzigingen doorgevoerd op het toestel.")
        print("  Controleer de foutmeldingen hierboven.")
        print("=" * 62)
        sys.exit(1)


if __name__ == "__main__":
    main()
