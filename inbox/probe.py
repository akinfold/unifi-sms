import re

import paramiko

from modem.client import ModemClient, parse_private_key
from .trust import key_fingerprint


class ProbeError(ValueError):
    pass


class InspectHostKey(paramiko.MissingHostKeyPolicy):
    """Allow a host key only within this explicit check; never persist it."""
    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)


def _read_command(ssh, command, limit=256):
    _, stdout, _ = ssh.exec_command(command, timeout=5)
    raw = stdout.read(limit + 1)
    if len(raw) > limit or stdout.channel.recv_exit_status() != 0:
        raise ProbeError("The device did not return valid identity information.")
    return raw.decode("utf-8", errors="replace").strip()


def probe_connection(values):
    """Authenticate and inspect one candidate without reading or deleting SMS."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(InspectHostKey())
    credentials = ({"password": values["password"]} if values["auth_method"] == "password"
                   else {"pkey": parse_private_key(values["private_key"])})
    try:
        ssh.connect(values["host"], port=values["port"], username=values["username"],
                    look_for_keys=False, allow_agent=False, timeout=6,
                    banner_timeout=6, auth_timeout=6, **credentials)
        transport = ssh.get_transport()
        key = transport.get_remote_server_key()
        address = transport.sock.getpeername()[0]
        model = _read_command(ssh, "sed -n '/^board.name=/{s/^board.name=//;p;q;}' /etc/board.info | cut -c 1-128")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}", model):
            raise ProbeError("Could not identify the device model.")
        if not re.fullmatch(r"U-?LTE(?:[ ._-]?(?:PRO|BACKUP))?(?:[ ._-](?:EU|US))?", model, re.IGNORECASE):
            raise ProbeError("This device is not a supported UniFi LTE device.")
        try:
            name = _read_command(ssh, "hostname | cut -c 1-128")
        except ProbeError:
            name = ""
        name = "".join(c for c in name if c.isprintable())[:128]
        result = {"address": address, "model": model, "name": name,
                  "numbers": [], "fingerprint": key_fingerprint(key),
                  "key_type": key.get_name(), "host_key": f"{key.get_name()} {key.get_base64()}"}
    except ProbeError:
        raise
    except paramiko.AuthenticationException:
        raise ProbeError("SSH authentication failed. Check the username and selected credential.") from None
    except (paramiko.SSHException, OSError, EOFError, ValueError):
        raise ProbeError("Could not connect to the device. Check its address, SSH port, and SSH access.") from None
    finally:
        ssh.close()
    lookup = values["host"] if values["port"] == 22 else f'[{values["host"]}]:{values["port"]}'
    try:
        with ModemClient(host=values["host"], port=values["port"], username=values["username"],
                         auth_method=values["auth_method"], password=values.get("password", ""),
                         private_key=values.get("private_key", ""), key_filename="", known_hosts="",
                         host_keys=f'{lookup} {result["host_key"]}', timeout=6) as modem:
            result["numbers"] = modem.phone_numbers()
    except Exception:
        result["phone_unavailable"] = True
    return result
