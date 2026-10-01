# Host side: the restricted tux2lab wrapper

The orchestrator container reaches tux2lab over SSH as the host account
`lab-orchestrator`. That account's key can do one thing only: run
`lab-orchestrator-wrapper`, which accepts five tux2lab commands and refuses
everything else (architecture doc §11, implementation plan M10).

```
container ──ssh──▶ lab-orchestrator@host ──forced command──▶ wrapper
                                         ──sudo -u <lab user>──▶ tux2lab vm …
```

| File | Installed as |
| --- | --- |
| `lab-orchestrator-wrapper` | `/usr/local/lib/lab-orchestrator/wrapper` |
| `wrapper.conf.example` | `/etc/lab-orchestrator/wrapper.conf` |
| `images.conf.example` | `/etc/lab-orchestrator/images.conf` |
| `stand-in/tux2lab` | **test machines only**, never next to a real tux2lab |

All steps run on the host, from a checkout of this repo (on the VPS:
`/opt/lab_orchestrator`). `LAB_USER` below is the account tux2lab runs as;
`TUX2LAB` is the CLI's path as that user.

## 1. Wrapper and its configuration

```bash
cd /opt/lab_orchestrator
LAB_USER=hermann                                 # adjust
TUX2LAB=$(sudo -u "$LAB_USER" -i command -v tux2lab); echo "$TUX2LAB"

sudo install -d -m 755 /usr/local/lib/lab-orchestrator /etc/lab-orchestrator
sudo install -m 755 deploy/host/lab-orchestrator-wrapper /usr/local/lib/lab-orchestrator/wrapper
sudo install -m 644 deploy/host/wrapper.conf.example /etc/lab-orchestrator/wrapper.conf
sudo install -m 644 deploy/host/images.conf.example /etc/lab-orchestrator/images.conf
sudo sed -i "s|^TUX2LAB_USER=.*|TUX2LAB_USER=$LAB_USER|; s|^TUX2LAB_BIN=.*|TUX2LAB_BIN=$TUX2LAB|" \
  /etc/lab-orchestrator/wrapper.conf
```

Edit `/etc/lab-orchestrator/images.conf` so every `tux2lab_image` in
`config/machines.yaml` maps to a golden image the host has
(`tux2lab golden-image list`). Everything stays root-owned, so the
`lab-orchestrator` account can't change what it is allowed to run.

## 2. The account and its key

```bash
sudo useradd --system --create-home --shell /bin/bash lab-orchestrator
sudo passwd -l lab-orchestrator

# The orchestrator's key, created where compose mounts the secrets.
mkdir -p deploy/secrets
ssh-keygen -t ed25519 -N '' -C lab-orchestrator -f deploy/secrets/tux2lab_ssh_key

sudo install -d -m 700 -o lab-orchestrator -g lab-orchestrator ~lab-orchestrator/.ssh
echo "restrict,command=\"/usr/local/lib/lab-orchestrator/wrapper\" $(cat deploy/secrets/tux2lab_ssh_key.pub)" \
  | sudo tee ~lab-orchestrator/.ssh/authorized_keys >/dev/null
sudo chown lab-orchestrator: ~lab-orchestrator/.ssh/authorized_keys
sudo chmod 600 ~lab-orchestrator/.ssh/authorized_keys
```

The shell must be a real one (sshd starts the forced command through it);
`restrict` still forbids a pty, forwarding and agent access.

## 3. The sudoers rule

One rule: `lab-orchestrator` may run the tux2lab CLI as the lab user, with
`vm` as the first argument, and nothing else.

```bash
echo "lab-orchestrator ALL=($LAB_USER) NOPASSWD: $TUX2LAB vm *" \
  | sudo tee /etc/sudoers.d/lab-orchestrator >/dev/null
sudo chmod 440 /etc/sudoers.d/lab-orchestrator
sudo visudo -cf /etc/sudoers.d/lab-orchestrator
```

## 4. Host key for the container

The container connects to `host.docker.internal` and checks the host key
against this file (never disabled):

```bash
ssh-keyscan -t ed25519 127.0.0.1 2>/dev/null \
  | sed 's/^127.0.0.1/host.docker.internal/' > deploy/secrets/known_hosts
sudo chown 10001 deploy/secrets/tux2lab_ssh_key deploy/secrets/known_hosts
sudo chmod 400 deploy/secrets/tux2lab_ssh_key
```

(uid 10001 is the user inside the orchestrator image.)

## 5. Network

- **Container → host sshd.** The host's sshd and firewall must accept the
  Docker network's subnet on port 22. Find it with
  `docker network inspect <LAB_NETWORK> --format '{{(index .IPAM.Config 0).Subnet}}'`;
  with ufw: `sudo ufw allow from <subnet> to any port 22 proto tcp`.
- **Container → lab VMs (TCP/22 readiness probe, later guacd).** libvirt
  rejects new connections forwarded into `labbr0`; allow the Docker subnet
  explicitly (architecture doc §17), e.g.
  `sudo iptables -I DOCKER-USER -s <subnet> -d 10.28.28.0/22 -p tcp --dport 22 -j ACCEPT`
  (make it persistent the way the host keeps its other rules).

## 6. Check by hand

```bash
K=deploy/secrets/tux2lab_ssh_key
sudo ssh -i $K -o IdentitiesOnly=yes lab-orchestrator@127.0.0.1 tux2lab vm list     # the table
sudo ssh -i $K -o IdentitiesOnly=yes lab-orchestrator@127.0.0.1 tux2lab vm stop -H x # refused, exit 126
sudo ssh -i $K -o IdentitiesOnly=yes lab-orchestrator@127.0.0.1 id                   # refused, exit 126
sudo journalctl -t lab-orchestrator-wrapper -n 20                                    # RUN / REFUSED lines
```

(`sudo` only because the key file now belongs to uid 10001.)

Then set `LAB_ORCH_TUX2LAB_BACKEND=ssh` in `deploy/.env` and restart the
stack (`docker compose … up -d`).

## Wrapper contract

| Request | Runs |
| --- | --- |
| `tux2lab vm list` | `tux2lab vm list` |
| `tux2lab vm info -H <h>` | `tux2lab vm info -H <h>` |
| `tux2lab vm start -H <h>` | `tux2lab vm start -H <h>` |
| `tux2lab vm remove -H <h>` | `tux2lab vm remove -H <h> -f` |
| `tux2lab vm install -H <h> -i <image>` | `tux2lab vm install -H <h> -d <distro> -v <version>` |

`<h>`: `^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`; `<image>`:
`^[A-Za-z0-9_.-]{1,128}$` and listed in `images.conf`. Requests may contain
only letters, digits, `_ . -` and single spaces. Anything else exits 126
with `lab-orchestrator-wrapper: refused: …` on stderr. tux2lab runs with
stdin from `/dev/null`; its stdout, stderr and exit code are passed through
unchanged (tux2lab prints errors to stdout). Every call is logged to syslog
under the tag `lab-orchestrator-wrapper`.

## Rehearsing without tux2lab

`stand-in/tux2lab` reproduces the five commands' output (from the tux2lab
source) for machines that can't run tux2lab, e.g. the test VM, which sits
inside a tux2lab lab network itself. Install it as the "CLI" in step 1 and
set its environment in a launcher script; see its header for the settings.
