# tux2lab output samples

Produced by `deploy/host/stand-in/tux2lab`, which copies the real CLI's
formatting from the tux2lab source (@ 0d0c7dc). ANSI color codes are kept,
as the orchestrator receives them. Replace these with output captured on the
real host when M10 reaches the VPS (implementation plan M10), and keep the
file names so the parser tests pick them up.

| File | Command | Situation |
| --- | --- | --- |
| `vm_list.txt` | `vm list` | one VM healthy, one booting, one shut off |
| `vm_info_healthy.txt` | `vm info -H lab-m01-aurora-7k4m2` | running, SSH reachable |
| `vm_info_ssh_not_accessible.txt` | `vm info -H lab-m02-forge-q3x9d` | running, still booting |
| `vm_info_shut_off.txt` | `vm info -H golden-ref-alma10` | powered off |
| `vm_info_unknown.txt` | `vm info -H no-such-vm` | no such VM; exits 0 |
| `vm_install_exists.txt` | `vm install` of an existing name | exits 1 |
