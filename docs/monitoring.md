# Container Monitoring

Updating containers is only half of it. The other half is noticing when one
dies — and being told something you can act on rather than "it restarted".

Monitoring is on by default (`MONITOR=false` turns it off) and watches
**every managed host**, not just the local one.

## What raises an alert

| Event | When |
|---|---|
| **unhealthy** | a healthcheck flips healthy → unhealthy and is *still* unhealthy on the next pass |
| **recovered** | it goes back to healthy |
| **exited** | a container stops on its own with a non-zero exit code |
| **crash_restart** | its restart policy brought it back — `RestartCount` went up |
| **oom** | the kernel's OOM killer took it |

A healthy → unhealthy flip does not alert on its own. It becomes *pending*
and only fires if the container is still unhealthy on the next sweep, which
is what keeps a container that wobbles for ten seconds during startup from
paging you. Each `(container, event)` pair is then quiet for 30 minutes.

A clean exit — code 0 — says nothing. That is a container finishing its
job, not dying.

## What an alert tells you

```
🔁 unifi crashed (exit 137) and was restarted by its restart policy at 16:14:47 (restart #1).
Host memory (used/total): 14.8/15.6 GB · Swap 3.9/4.0 GB
Top memory at event time: some-new-app 9.1GiB · unifi 2.2GiB
Top CPU at event time: some-new-app 198%
Last logs:
…
```

**The exit code is real.** `docker inspect` cannot supply it for a
crash-restart — the policy has the container running again by the time
anything looks, and a running container reports `ExitCode: 0`. It comes
from the runtime's live event stream instead.

**The resource lines name the culprit, not just the victim.** A container
squeezed out by a neighbour frequently dies *without* the kernel flagging
an OOM, so these are attached to every death rather than only to OOM kills.

The host line comes first on purpose. A list of top consumers on its own
invites you to blame whoever is at the top — and if there were eight
gigabytes free, that is just your biggest container minding its own
business.

**CPU appears only when something is actually holding the processor.** That
line exists because CPU starvation kills in a way that looks exactly like
running out of memory. Measured: the same container, the same shutdown
handler, the same `docker stop -t 5` —

```
--cpus=1.0    → exit 0, clean
--cpus=0.005  → exit 137, OOMKilled false
```

Starved of CPU it could not answer SIGTERM inside its grace period, so
Docker escalated to SIGKILL. Exit 137 with no OOM flag: indistinguishable
from a kernel OOM kill unless something tells you who was holding the CPU.

## Taken at the moment of death

The monitor polls every `MONITOR_INTERVAL` seconds (60 by default). If the
resource snapshot were taken then, it could be a full minute stale — by
which time the container that ate the memory may have released it, been
restarted, or be the one you killed in a panic.

So Docksentry also watches the runtime's live event stream. The `die` event
arrives essentially instantly, and the snapshot is taken there: measured at
**0.08 seconds** after the death rather than at the next sweep.

```yaml
environment:
  - MONITOR_EVENTS=false     # turn the watcher off; polling still works
```

The watcher never sends anything itself. Alerting stays with the poller,
which owns the debounce, the cooldown and the event log — the watcher only
records evidence the poller picks up. If it cannot start, Docksentry falls
back to polling exactly as before.

One measured difference worth knowing if you run Podman: rootless Podman
reported **no OOM event at all** for a container the kernel killed at its
own 20 MB ceiling, and `OOMKilled` was false in `inspect` too — the same
death Docker reported as an OOM twice over. That is why the resource
snapshot hangs off every death rather than off the OOM flag.

## Where events go

Everything is written to a persistent log, so "what happened last night?"
is answerable without scrollback:

- Web UI → **Events** section on the status page
- Telegram / Discord → `/events`

Alerts respect quiet hours and maintenance mode, and stay silent while an
update is running — containers bounce legitimately then, and diffing across
an update window would read every recreate as a crash.

## Host memory

The status page shows host memory beside host storage. It reads
`/proc/meminfo`, which inside a container still reports the *host's*
figures.

Shown for the local host only. That file describes the machine Docksentry
runs on, so printing it under a remote host's name would be a plain lie —
remote monitors leave the line out rather than show a number from the wrong
machine.
