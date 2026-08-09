# Podman

Docksentry runs on Podman as well as Docker. The [Quick Start section in
the README](../README.md#podman-support) has the two socket recipes
(rootful and rootless) and the list of known limitations; this page is the
part that goes beyond mounting a socket — which CLI gets driven, what
socket activation actually does for us, and how remote Podman hosts work.

Everything stated here was measured against **podman 4.9.3** (and, where
the two are compared, **docker 29.5.3**) on Linux. Where a claim is a
recommendation rather than a measurement, it says so.

## Which CLI Docksentry drives — `CONTAINER_CLI`

`CONTAINER_CLI` takes `auto` (the default), `docker` or `podman`.

- **`docker` / `podman`** force the choice. Docksentry then shells out to
  exactly that binary for every operation: checks, pulls, recreates,
  rollback, lifecycle, compose, cleanup.
- **`auto`** picks `docker` whenever a `docker` command exists on
  `PATH` — *including* the `docker` → `podman` alias or shim that Podman
  users have been installing for years — and only falls back to `podman`
  when `docker` is genuinely absent. That ordering is deliberate: it turns
  a previously broken install into a working one without changing any
  install that already worked.

So on a machine with a `docker` shim you do not have to set anything, and
setting `CONTAINER_CLI=podman` is still the cleaner statement of intent —
it removes the shim from the path entirely.

One thing `CONTAINER_CLI` does **not** cover: **self-update**. Docksentry
updating *itself* launches a `docker:cli` helper container, because it
cannot run inside the container it is replacing, so that one path still
needs `docker` to resolve. Everything else follows the setting.

## The rootless socket, and why socket activation suits this

```bash
systemctl --user enable --now podman.socket
# creates /run/user/$UID/podman/podman.sock
```

Note what that command enables: the **socket**, not the service. Measured
on a machine that has been up for a week:

```
$ systemctl --user is-enabled podman.socket    → enabled
$ systemctl --user is-enabled podman.service   → disabled
$ systemctl --user status podman.socket
  Active: active (running) since Sat 2026-08-01 17:43:09 CEST
  Triggers: ● podman.service
```

`podman.service` is *disabled* and yet had been running — it cannot have
been started at boot, so systemd started it on the first request that
arrived at the socket. That is the whole mechanism: the socket file exists
from the moment you enable it, nothing is running behind it, and the first
client to connect causes systemd to start the API service.

It goes away again too. `podman system service` takes `--time`, an idle
timeout, **default 5 seconds** — and the unit shipped with Podman does not
override it:

```
$ systemctl --user cat podman.service
  ExecStart=/usr/bin/podman $LOGGING system service
```

Measured directly, with a service started by hand on a spare port: it
answered one request, then exited on its own about five seconds after the
last one. So an idle machine really does run no Podman API process at all.

**How much of that you actually get depends on your settings**, and it is
worth being straight about it rather than repeating the pleasant version:

- With `MONITOR=false` and no event watching, Docksentry only touches the
  socket on its scheduled check (`CRON_SCHEDULE`, once a day by default)
  and whenever you ask it something. The service is up for a few seconds
  a day. This is the case socket activation is made for.
- With the **defaults** (`MONITOR=true`, `MONITOR_INTERVAL=60`,
  `MONITOR_EVENTS=true`) it is not sporadic at all. Measured from the
  Podman journal over two hours on a monitored host: a `/events` stream
  held open, plus `_ping` and a container listing every 60 seconds. The
  idle timeout is reset long before it can fire, and the API service stays
  up permanently. That is monitoring working as designed — it is just not
  an idle machine any more.

Either way the socket route needs nothing from you beyond the unit above,
and it is the route that requires no `CONTAINER_CLI` change at all: Podman
serves the Docker REST API, so mounting its socket where Docksentry
expects Docker's is enough.

## Remote Podman hosts

Extra hosts go in `DOCKER_HOSTS` as `name:endpoint` pairs. Docksentry puts
the endpoint in front of every command it issues to that host —
`podman --url … ps`, `podman --url … pull`, and so on — so a remote host
behaves like the local one everywhere.

```yaml
environment:
  - CONTAINER_CLI=podman
  - DOCKER_HOSTS=nas:ssh://root@nas/run/podman/podman.sock, pve1:tcp://pve1:8888
```

A **TCP endpoint** (ideally a [socket proxy](security.md)) is the simplest
thing that works and has no key management at all. If you can use one,
stop reading here.

### SSH endpoints: Podman's key handling is not Docker's

This is the part that costs people an evening, so it is spelled out. All
of the following was measured against a local `sshd` whose
`authorized_keys` accepted one throwaway key and *not* the account's
default `~/.ssh/id_ed25519`:

| | `docker -H ssh://…` | `podman --url ssh://…` |
|---|---|---|
| ssh client used | the real `ssh` binary | Go's built-in ssh |
| honours `~/.ssh/config` | **yes** | **no** |
| key comes from | `~/.ssh/config`, agent, defaults | `--identity`, `CONTAINER_SSHKEY`, or a stored connection |

Adding a `Host … IdentityFile …` block to `~/.ssh/config` turned Docker's
`Permission denied (publickey,password).` into a container listing. The
same block changed nothing for Podman — not in the default `--ssh golang`
mode and not under `--ssh native`.

Worse, and this is the trap: **`podman --url` silently borrows the
identity of whichever stored connection is the *default* one**, whatever
host that connection points at. It is not matched by URL. Measured with
two connections on the *same* URI, the default one carrying an
unauthorised key and a second one carrying the right key:

```
$ podman --url ssh://user@host/run/user/1000/podman/podman.sock ps
Error: unable to connect to Podman socket: failed to connect: ssh:
handshake failed: ssh: unable to authenticate, attempted methods
[none publickey], no supported methods remain: ssh://user@host/…

$ podman --connection the-second-one ps
CONTAINER ID  IMAGE   …
```

So on a machine that has more than one stored connection, a URL endpoint
cannot be pointed at the right key at all. `CONTAINER_SSHKEY` is not a way
out either: it is one key for the whole process — every host would share
it — and it loses to the default connection's identity when one exists.
Both measured.

### `context://` — name a stored connection instead

Because of the above, an endpoint in `DOCKER_HOSTS` may name a **stored
connection** rather than a URL:

```bash
podman system connection add --identity ~/.ssh/id_nas \
    nas ssh://root@nas/run/podman/podman.sock
```

```yaml
environment:
  - CONTAINER_CLI=podman
  - DOCKER_HOSTS=nas:context://nas
```

Docksentry then issues `podman --connection nas …`, which uses *that*
connection's own `--identity`. This is the only arrangement that gives
each remote Podman host its own key.

The same spelling works on Docker, where the stored thing is called a
context: `context://nas` becomes `docker --context nas …`. (Podman accepts
`podman context ls` as an alias for its connection list, which is why one
word covers both here.)

Two things to know about it:

- The name must exist. An unknown one fails loudly — it does **not**
  quietly fall back to the local socket, which would report the wrong
  machine's containers under a remote host's name.
- The connection lives in the home directory of the user running the
  CLI. In a container that is the *container's* home directory, so either
  mount `~/.config/containers/containers.conf` in, or use the
  `ssh://` + `~/.ssh/config` route on Docker, or a TCP endpoint. *(A
  recommendation, not a measurement: which of the three fits depends on
  how you run Docksentry.)*

### When a host shows as unreachable

The Status page prints the CLI's own last line of output under the
`host: unreachable` row, because "unreachable" alone is the same word for
four quite different problems. What each one looks like on Podman:

| What you see | What it means |
|---|---|
| `ssh: unable to authenticate, attempted methods [none publickey]` | the key was refused — see above |
| `ssh: handshake failed: EOF` | no usable key was offered at all |
| `dial tcp …: connect: connection refused` | nothing is listening on that port |
| `dial tcp: lookup …: no such host` | DNS, or a typo in the host name |
| `ssh: rejected: connect failed (open failed)` | ssh got in; the socket path in the URL is wrong |

Podman prints a line *above* all of these suggesting `podman machine init`
and `podman machine start`. Ignore it for a remote host — `podman machine`
is about a local VM and has nothing to do with an ssh endpoint. Docksentry
deliberately shows only the last line for that reason.

## Pods

Containers inside a Podman **pod** are handled correctly as of **v2.6.0**.
Before that, no update on Podman could succeed — three separate defects in
rebuilding the run command, one of which specifically mangled pod members
into `--network container:<infra-id>`, which Podman refuses. The full
account, including what was measured and the fact that nothing was ever
destroyed by it, is in the [CHANGELOG under 2.6.0](../CHANGELOG.md). If
you are on an older version and update containers on Podman, that is the
release to get to.

## `io.containers.autoupdate`

Podman has an updater of its own: a container labelled
`io.containers.autoupdate=registry` (or `local`) is picked up by
`podman auto-update`, normally on a systemd timer.

Docksentry **reads that label and reports it, and does nothing about it**.
The container gets a `podman auto-update` badge on the Status page,
saying that two updaters now have an opinion about it and whichever fires
first wins. Docksentry will still update it if you ask — the badge is
information, not a lock.

If you would rather have exactly one updater on such a container, pick
one:

- leave it to Podman and add the container to `MONITOR_ONLY_CONTAINERS`
  (or label it `docksentry.monitor-only=true`) — it stays visible and
  still reports available updates, it is just never recreated here;
- or leave it to Docksentry and drop the `io.containers.autoupdate` label.

The same badge and the same reasoning apply to quadlets, where systemd
owns the container — see [Update Workflow](updates.md) for that.
