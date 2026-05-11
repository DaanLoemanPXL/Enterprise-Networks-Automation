#!/usr/bin/env python3
import os
import sys
import requests
from lxml import etree
from ncclient import manager
from ncclient.operations.rpc import RPCError
from ncclient.operations.errors import TimeoutExpiredError
from ncclient.transport.errors import AuthenticationError, SSHError

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION  –  edit here or use environment variables
# ─────────────────────────────────────────────────────────────
DEVICE = {
    "host":     os.getenv("NC_HOST",     "172.17.7.65"),
    "port":     int(os.getenv("NC_PORT", "830")),
    "username": os.getenv("NC_USER",     "admin"),
    "password": os.getenv("NC_PASS",     "cisco123"),
    "timeout":  int(os.getenv("NC_TIMEOUT", "30")),
    "hostkey_verify": False,
}

GITHUB_RAW_URL = os.getenv(
    "GITHUB_CONFIG_URL",
    "https://raw.githubusercontent.com/DaanLoemanPXL/Enterprise-Networks-Automation/main/iosxe_router_config.xml"
)

# NETCONF datastore to target: "running" | "candidate" | "startup"
DATASTORE = os.getenv("NC_DATASTORE", "candidate")
# ─────────────────────────────────────────────────────────────


# ── ANSI colour helpers ───────────────────────────────────────
def ok(msg):    print(f"\033[92m[OK]    {msg}\033[0m")
def warn(msg):  print(f"\033[93m[WARN]  {msg}\033[0m")
def err(msg):   print(f"\033[91m[ERROR] {msg}\033[0m")
def info(msg):  print(f"\033[94m[INFO]  {msg}\033[0m")


# ── Step 1 – Pull configuration from GitHub ──────────────────
def fetch_config_from_github(url: str) -> str:
    info(f"Fetching configuration from GitHub …")
    info(f"  URL: {url}")
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        ok(f"Configuration pulled successfully  "
           f"(HTTP {response.status_code}, {len(response.content)} bytes)")
        return response.text
    except requests.exceptions.HTTPError as e:
        err(f"HTTP error while fetching config: {e}")
        err(f"  HTTP Status Code : {e.response.status_code}")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        err(f"Connection error – cannot reach GitHub: {e}")
        sys.exit(1)
    except requests.exceptions.Timeout:
        err("Request timed out while contacting GitHub.")
        sys.exit(1)


# ── Step 2 – Validate that the payload is well-formed XML ─────
def validate_xml(xml_text: str) -> etree._Element:
    info("Validating XML structure …")
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
        ok(f"XML is well-formed  (root tag: <{root.tag}>)")
        return root
    except etree.XMLSyntaxError as e:
        err(f"XML parse error: {e}")
        sys.exit(1)


# ── Step 3 – Parse NETCONF RPC-reply for <ok> or <rpc-error> ─
def parse_rpc_reply(reply):
    """
    Returns (success: bool, message: str).
    ncclient raises RPCError on <rpc-error>, but we also inspect
    the raw XML so the user sees every detail.
    """
    ns = "urn:ietf:params:xml:ns:netconf:base:1.0"
    try:
        root = etree.fromstring(reply.xml.encode("utf-8"))
    except Exception:
        return False, "Could not parse RPC reply XML."

    # <ok/> means success
    if root.find(f".//{{{ns}}}ok") is not None:
        return True, "<ok/> received – operation completed successfully."

    # Collect all <rpc-error> blocks
    errors = root.findall(f".//{{{ns}}}rpc-error")
    if errors:
        messages = []
        for rpc_err in errors:
            etype   = _tag_text(rpc_err, f"{{{ns}}}error-type",     "unknown")
            etag    = _tag_text(rpc_err, f"{{{ns}}}error-tag",      "unknown")
            esev    = _tag_text(rpc_err, f"{{{ns}}}error-severity",  "unknown")
            emsg    = _tag_text(rpc_err, f"{{{ns}}}error-message",   "")
            epath   = _tag_text(rpc_err, f"{{{ns}}}error-path",      "")
            block = (
                f"  error-type    : {etype}\n"
                f"  error-tag     : {etag}\n"
                f"  error-severity: {esev}\n"
                f"  error-message : {emsg}"
            )
            if epath:
                block += f"\n  error-path    : {epath}"
            messages.append(block)
        return False, "\n".join(messages)

    return False, "Unknown reply – neither <ok/> nor <rpc-error> found."


def _tag_text(parent, tag, default=""):
    el = parent.find(tag)
    return el.text.strip() if el is not None and el.text else default


# ── Step 4 – Connect and apply configuration ──────────────────
def apply_config(xml_text: str):
    info(f"Connecting to {DEVICE['host']}:{DEVICE['port']} via NETCONF …")
    try:
        with manager.connect(
            host=DEVICE["host"],
            port=DEVICE["port"],
            username=DEVICE["username"],
            password=DEVICE["password"],
            timeout=DEVICE["timeout"],
            hostkey_verify=DEVICE["hostkey_verify"],
            device_params={"name": "default"},  # use "iosxr", "junos", "eos" etc. if known
        ) as conn:
            ok(f"NETCONF session established  "
               f"(session-id: {conn.session_id})")

            # ── Edit-config ──────────────────────────────────
            info(f"Sending edit-config to datastore: [{DATASTORE}] …")
            try:
                reply = conn.edit_config(
                    target=DATASTORE,
                    config=xml_text
                )
                success, message = parse_rpc_reply(reply)
                if success:
                    ok(f"edit-config reply: {message}")
                else:
                    err(f"edit-config returned an error:\n{message}")
                    sys.exit(1)

            except RPCError as e:
                err("NETCONF RPC error during edit-config:")
                err(f"  tag      : {e.tag}")
                err(f"  type     : {e.type}")
                err(f"  severity : {e.severity}")
                err(f"  message  : {e.message}")
                sys.exit(1)

            # ── Commit (only relevant for candidate datastore) ─
            if DATASTORE == "candidate":
                info("Committing candidate configuration …")
                try:
                    reply = conn.commit()
                    success, message = parse_rpc_reply(reply)
                    if success:
                        ok(f"commit reply: {message}")
                    else:
                        err(f"commit returned an error:\n{message}")
                        sys.exit(1)
                except RPCError as e:
                    err("NETCONF RPC error during commit:")
                    err(f"  tag      : {e.tag}")
                    err(f"  severity : {e.severity}")
                    err(f"  message  : {e.message}")
                    sys.exit(1)

            ok("Configuration deployment finished successfully.")

    except AuthenticationError:
        err(f"Authentication failed for user '{DEVICE['username']}' on {DEVICE['host']}.")
        sys.exit(1)
    except SSHError as e:
        err(f"SSH/transport error: {e}")
        sys.exit(1)
    except TimeoutExpiredError:
        err(f"Connection timed out after {DEVICE['timeout']}s.")
        sys.exit(1)
    except Exception as e:
        err(f"Unexpected error: {type(e).__name__}: {e}")
        sys.exit(1)


# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  NETCONF Configuration Deployment Tool")
    print("=" * 60)

    xml_text = fetch_config_from_github(GITHUB_RAW_URL)
    validate_xml(xml_text)
    apply_config(xml_text)

    print("=" * 60)
    ok("All steps completed.")
    print("=" * 60)