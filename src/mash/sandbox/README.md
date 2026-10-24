# Sandbox

`src/mash/sandbox` holds per-user sandboxes: where the `bash` commands an
agent's model writes run. A sandbox belongs to one user and holds that user's
workspaces, one folder per agent at `/home/agent/workspaces/<agent_id>`. It has
no secrets, no database access, and gets no environment from the host.

## Call path

```
agent process             host                          sandbox u-42
BashTool.execute ─► POST /sandbox/exec ─► SandboxManager ─► POST /exec
   (agent token,       request identity     ensure(u-42)      bash -c as agent
    request_id)        user = u-42                            in workspaces/research
```

The agent sends only its request id. The host's relay
(`mash/api/routes/sandbox.py`) reads the request's identity, checks it belongs
to the calling agent, and runs the command for that request's user.

## Main components

- `daemon.py`: `SandboxDaemon`, the app `mash sandbox serve` runs inside a
  sandbox as the `mash` user. `POST /exec` takes `{agent_id, command, timeout}`,
  creates the agent's folder, runs `bash -c` there as `agent` through `sudo`
  with a clean environment (`HOME`, `PATH`, `LANG`, `PWD`), and returns
  `{output, exit_code, timed_out}`. Each call is a fresh shell. Output is cut to
  50 lines and 4000 characters; the default timeout is 30 seconds, after which
  the command's whole process group is killed. A job the command leaves running
  in the background does not hold the call open. Every route but `/health`
  needs the sandbox token.
- `tokens.py`: `derive_sandbox_token(secret, user_id)`, the HMAC of
  `mash-sandbox:<user_id>` under `MASH_HOST_SECRET`. Only the host can derive
  it, and one user's token opens no other sandbox.
- `provider.py`: the `SandboxProvider` protocol (`ensure`, `stop`, `list`,
  `close`) and `DockerSandboxProvider`: container and volume
  `<ns>-sandbox-<key>` (`<key>` is 16 hex characters of the user id's sha256,
  since Docker names allow no `@` or `:`), labels `mash.sandbox.user` and
  `mash.namespace`, network `<ns>-sandboxes`, restart policy `unless-stopped`.
  A host in a container joins the network and reaches daemons by name and
  sandboxes run its image; a host on the Docker host publishes each daemon's
  port on `127.0.0.1` and needs `MASH_SANDBOX_IMAGE`. `MASH_SANDBOX_MOUNTS`
  adds `source:target[:ro]` bind mounts; `MASH_SANDBOX_NAMESPACE` replaces
  `mash` in the names.
- `manager.py`: `SandboxManager`, the host's view. It starts a user's sandbox
  under a per-user lock on first exec, stops one with nothing in flight and no
  exec for `MASH_SANDBOX_IDLE_SECONDS` (default 900), and keeps at most
  `MASH_SANDBOX_MAX_RUNNING` (default 20) running, stopping the least recently
  used idle one to make room or raising `SandboxCapacityError` when all are
  busy. After a host restart, running sandboxes count as active from then.
- `docker_engine.py`: the minimal async Docker Engine API client over the Unix
  socket (`DOCKER_HOST`, `/var/run/docker.sock`, or Docker Desktop's
  `~/.docker/run/docker.sock`).

`mash.testing.sandboxes.LocalSandboxProvider` runs daemons in-process on
localhost ports with a temporary directory per user, for tests.
