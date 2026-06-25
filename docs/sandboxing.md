# Sandboxing knovas-extract

`knovas-extract` is hardened against the attack patterns we know about, but a defense-in-depth strategy for untrusted documents includes process-level isolation. This doc has copy-paste recipes for the three sandboxes we recommend and have tested.

**Threat model**: an attacker controls the input bytes you pass to `extract()`. We assume PyMuPDF / python-docx / etc. *might* have an unknown RCE-grade bug; sandboxing contains the blast radius even if the parser is compromised.

## tl;dr decision matrix

| You're running on | Use |
|---|---|
| Linux, full control over the host | **bubblewrap** (rootless, light, fast) |
| Linux, untrusted multi-tenant (e.g. SaaS extracting customer docs) | **nsjail** (cgroup limits + seccomp + namespaces) |
| Anywhere with Docker / Podman | **rootless container** |
| macOS / Windows | OS-native sandboxing is weak — strongly consider extracting in a Linux VM |

## bubblewrap (recommended for single-tenant use)

```bash
# Install on Debian/Ubuntu: sudo apt install bubblewrap
bwrap \
  --unshare-all \
  --share-net no \
  --die-with-parent \
  --proc /proc \
  --tmpfs /tmp \
  --ro-bind / / \
  --bind /path/to/the/document.pdf /tmp/in.pdf \
  --bind /path/to/output.json /tmp/out.json \
  --chdir /tmp \
  --setenv HOME /tmp \
  /usr/bin/python -m knovas_extract /tmp/in.pdf > /tmp/out.json
```

Flags explained:

- `--unshare-all --share-net no` — separate namespaces; no network, no PID visibility, no IPC.
- `--die-with-parent` — orphaned child gets SIGKILL when the launcher exits.
- `--tmpfs /tmp` — every write goes to an in-memory filesystem that vanishes on exit.
- `--ro-bind / /` — root filesystem read-only.
- The only writable thing is `/tmp`; the only readable input is the explicit `--bind` of the document.

## nsjail (recommended for multi-tenant SaaS)

`nsjail` (Google) adds CPU/memory cgroup caps and a seccomp filter on top of namespace isolation. Recommended config:

```jsonc
// extract.nsjail.cfg
name: "knovas-extract"
description: "isolated document extraction"

mode: ONCE
hostname: "extract"
cwd: "/tmp"

uidmap { inside_id: "1000" outside_id: "1000" }
gidmap { inside_id: "1000" outside_id: "1000" }

mount {
  src: "/usr"        dst: "/usr"        is_bind: true is_ro: true
}
mount {
  src: "/lib"        dst: "/lib"        is_bind: true is_ro: true
}
mount {
  src: "/lib64"      dst: "/lib64"      is_bind: true is_ro: true mandatory: false
}
mount {
  dst: "/tmp"        fstype: "tmpfs"    rw: true options: "size=128m,mode=0700"
}
mount {
  src: "/path/to/input.pdf" dst: "/tmp/in.pdf" is_bind: true is_ro: true
}

rlimit_as: 512   # MB virtual memory
rlimit_cpu: 30   # seconds
rlimit_fsize: 32 # MB output

seccomp_string: "ALLOW { read, write, mmap, munmap, brk, openat, close, fstat, lseek, futex, getrandom, exit_group } DEFAULT KILL"

exec_bin: { path: "/usr/bin/python3" arg: "-m" arg: "knovas_extract" arg: "/tmp/in.pdf" }
```

Then:

```bash
nsjail --config extract.nsjail.cfg > out.json
```

The seccomp filter is intentionally tight; for formats that need more syscalls (PDF parsing triggers `clone` for thread pools), extend the allow-list rather than dropping seccomp entirely.

## Rootless container

If you already run containers, this is the lowest-friction option:

```bash
# Use rootless mode (podman is rootless by default; docker needs setup).
podman run --rm \
  --read-only --tmpfs /tmp:size=128M \
  --network=none \
  --memory=512m --cpus=1 \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --security-opt=seccomp=./seccomp-extract.json \
  -v "$PWD/input.pdf:/tmp/in.pdf:ro" \
  ghcr.io/knovas/knovas-extract:<VERSION> \
  knovas-extract /tmp/in.pdf
```

A reference `seccomp-extract.json` ships in `extras/seccomp-extract.json` of the release archive.

## What sandboxing does NOT protect against

- **Data exfiltration through your output channel.** If you log the extracted text and an attacker compromises the parser, the text might be hostile (e.g. crafted to trigger a downstream injection). Treat `result.content.text` as untrusted input for any consumer.
- **Timing / resource side channels.** A document that takes 10× longer than usual to extract leaks information about its structure. Not addressable here.
- **Logic bugs in your application** that pass an attacker control over the file path argument. Validate paths before calling `extract()`.

## Disabling network at the OS level (belt + suspenders)

Even with `--share-net no`, defense in depth helps. On Linux, add a final iptables egress block scoped to the extraction user:

```bash
# Assuming the extraction process runs as user `extract` (uid 5001).
iptables -A OUTPUT -m owner --uid-owner 5001 -j REJECT --reject-with icmp-port-unreachable
```

`knovas-extract` makes zero network calls by design, so this rule is harmless to the library and catches *any* exfiltration attempt — including from compromised native parser code.
