import re
import socket

import paramiko

from modem.client import ModemClient, parse_host_keys, parse_private_key
from modem.ssh import SSHCommandError, read_command
from .trust import key_fingerprint


class ProbeError(ValueError):
    pass


def inspect_host_key(values):
    """Read the SSH host key without sending authentication credentials."""
    transport = None
    sock = None
    try:
        sock = socket.create_connection((values["host"], values["port"]), timeout=6)
        transport = paramiko.Transport(sock)
        transport.banner_timeout = 6
        transport.start_client(timeout=6)
        key = transport.get_remote_server_key()
        return {"address": sock.getpeername()[0], "fingerprint": key_fingerprint(key),
                "key_type": key.get_name(), "host_key": f"{key.get_name()} {key.get_base64()}",
                "identity_verified": False}
    except (paramiko.SSHException, OSError, EOFError, ValueError):
        raise ProbeError("Could not read the SSH host key. Check the address and SSH port.") from None
    finally:
        if transport is not None:
            transport.close()
        if sock is not None:
            sock.close()


def _read_command(ssh, command, limit=256):
    try:
        raw = read_command(ssh, command, timeout=5, limit=limit)
    except (SSHCommandError, TimeoutError):
        raise ProbeError("The device did not return valid identity information.") from None
    return raw.decode("utf-8", errors="replace").strip()


def probe_connection(values, *, host_key):
    """Verify the approved key before authenticating and reading device identity."""
    lookup = values["host"] if values["port"] == 22 else f'[{values["host"]}]:{values["port"]}'
    keys = parse_host_keys(f"{lookup} {host_key}")
    ssh = paramiko.SSHClient()
    for kind, key in keys.lookup(lookup).items():
        ssh.get_host_keys().add(lookup, kind, key)
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
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
                  "numbers": [], "identity_verified": True, "fingerprint": key_fingerprint(key),
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
