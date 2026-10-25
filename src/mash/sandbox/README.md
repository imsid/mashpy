# Sandbox

`src/mash/sandbox` holds per-user sandboxes: the computer where the code an
agent's model writes runs. A sandbox belongs to one user and holds that user's
workspaces, one folder per agent at `/home/agent/workspaces/<agent_id>`. It has
no secrets, no database access, and gets no environment from the host.

## Call paths

```
agent process             host                          sandbox u-42
run_code ─────────► POST /sandbox/exec ──► SandboxManager ─► POST /exec
sandbox_put/get ──► /sandbox/files ──────►   ensure(u-42) ─► GET/PUT /files
   (agent token,       request identity                       as agent, in
    request_id)        user = u-42                            workspaces/research

browser ──────────► /s/<link token>/files ─► SandboxManager ─► GET /files
                    /s/<link token>/ports ─►               ─► /ports/<n> ─► 127.0.0.1:<n>
                       user from the token
```

The agent sends only its request id. The host's relay
(`mash/api/routes/sandbox.py`) reads the request's identity, checks it belongs
to the calling agent, and acts for that request's user. A link names its user
in its token.

## Main components

- `daemon.py`: `SandboxDaemon`, the app `mash sandbox serve` runs inside a
  sandbox as the `mash` user. Every route but `/health` needs the sandbox
  token, and everything it does in a workspace it does as `agent` through
  `sudo`.
  - `POST /exec` takes `{agent_id, command, timeout, stdin?, env?}`, creates the
    agent's folder, runs `bash -c` there with `stdin` as input and a clean
    environment (`HOME`, `PATH`, `LANG`, `PWD`, `PYTHONUSERBASE=<folder>/.local`
    so `pip install --user` lands in the folder, plus the caller's `MASH_*`
    variables), and returns `{output, exit_code, timed_out}`. Each call is a
    fresh shell. Output is cut to 50 lines and 4000 characters; the default
    timeout is 30 seconds, after which the command's whole process group is
    killed. A job the command leaves running in the background does not hold
    the call open, and keeps running.
  - `GET` / `PUT /files/<agent_id>/<path>` read or write one file's bytes (up to
    100 MB) with `cat`, so files keep the agent's ownership. Paths must stay in
    the folder.
  - `/ports/<n>/<path>` proxies HTTP to `127.0.0.1:<n>`, buffered, without the
    `Authorization` header.
- `tokens.py`: `derive_sandbox_token(secret, user_id)`, the HMAC of
  `mash-sandbox:<user_id>` under `MASH_HOST_SECRET`. Only the host can derive
  it, and one user's token opens no other sandbox. `derive_link_token` and
  `user_for_link_token` make and read the `<base64url(user_id)>.<hmac>` tokens
  in users' links.
- `provider.py`: the `SandboxProvider` protocol (`ensure`, `stop`, `list`,
  `close`) and `DockerSandboxProvider`: container and volume
  `<ns>-sandbox-<key>` (`<key>` is 16 hex characters of the user id's sha256,
  since Docker names allow no `@` or `:`), labels `mash.sandbox.user` and
  `mash.namespace`, network `<ns>-sandboxes`, restart policy `unless-stopped`.
  Sandboxes run `ghcr.io/imsid/mash-sandbox:<mashpy version>`
  (`Dockerfile.sandbox`), pulled on first start, or `MASH_SANDBOX_IMAGE`. A
  host in a container joins the network and reaches daemons by name; a host on
  the Docker host publishes each daemon's port on `127.0.0.1`. `MASH_SANDBOX_MOUNTS`
  adds `source:target[:ro]` bind mounts; `MASH_SANDBOX_NAMESPACE` replaces
  `mash` in the names.
- `manager.py`: `SandboxManager`, the host's view, and the one way into a
  sandbox: `exec`, `read_file`, `write_file` and `proxy`. It starts a user's
  sandbox under a per-user lock on first use, stops one with nothing in flight
  and no use for `MASH_SANDBOX_IDLE_SECONDS` (default 900), and keeps at most
  `MASH_SANDBOX_MAX_RUNNING` (default 20) running, stopping the least recently
  used idle one to make room or raising `SandboxCapacityError` when all are
  busy. After a host restart, running sandboxes count as active from then.
- `docker_engine.py`: the minimal async Docker Engine API client over the Unix
  socket (`DOCKER_HOST`, `/var/run/docker.sock`, or Docker Desktop's
  `~/.docker/run/docker.sock`).

`mash.testing.sandboxes.LocalSandboxProvider` runs daemons in-process on
localhost ports with a temporary directory per user, for tests.
