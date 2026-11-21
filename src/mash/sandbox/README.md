# Sandbox

`src/mash/sandbox` holds per-policy sandboxes: the computer where the code an
agent's model writes runs. A sandbox belongs to one policy, shared by every
session under it, and holds that policy's workspaces, one folder per agent at `/home/agent/workspaces/<agent_id>`. It has
no secrets, no database access, and gets no environment from the host. A
command sees only the link variables and the `MASH_*` variables its agent's
spec declares in `sandbox_env()`, which `run_code` sends with each call.

## Call paths

```
agent process             host                          sandbox acme-ws
run_code ─────────► POST /sandbox/exec ──► SandboxManager ─► POST /exec
edit_file ───────┐
sandbox_put/get ─┴► /sandbox/files ──────►   ensure(acme-ws) ─► GET/PUT /files
   (agent token,       request identity                       as agent, in
    request_id)        policy = acme-ws                       workspaces/research

browser ──────────► /s/<link token>/files ─► SandboxManager ─► GET /files
                    /s/<link token>/ports ─►               ─► /ports/<n> ─► 127.0.0.1:<n>
                       policy from the token
```

The agent sends only its request id. The host's relay
(`mash/api/routes/sandbox.py`) reads the request's identity, checks it belongs
to the calling agent, and acts in that request's policy's sandbox. A request
whose policy snapshot says `"sandbox": false` is refused. A link names its
policy in its token.

## Main components

- `daemon.py`: `SandboxDaemon`, the app `mash sandbox serve` runs inside a
  sandbox as root. Every route but `/health` needs the sandbox token, and
  everything it does in a workspace it does as the calling agent's own uid,
  through `setpriv`, with the caller's variables in the environment, never in
  argv.
  - `POST /exec` takes `{agent_id, command, timeout, stdin?, env?}`, creates the
    agent's folder, runs `bash -c` there with `stdin` as input and a clean
    environment (`HOME`, `PATH`, `LANG`, `PWD`, `PYTHONUSERBASE=<folder>/.local`
    so `pip install --user` lands in the folder, plus the caller's `MASH_*`
    variables), and returns `{output, exit_code, timed_out}`. Each call is a
    fresh shell. Output is cut to 30000 characters; the default
    timeout is 30 seconds, after which the command's whole process group is
    killed. A job the command leaves running in the background does not hold
    the call open, and keeps running.
  - `GET` / `PUT /files/<agent_id>/<path>` read or write one file's bytes (up to
    100 MB) with `cat`, so files keep the agent's ownership. Paths must stay in
    the folder.
  - `/ports/<n>/<path>` proxies HTTP to `127.0.0.1:<n>`, streaming both
    bodies, without the `Authorization` header.
- `agent_users.py`: `AgentUsers`, a uid for each agent from 20000 up, mapped
  in `<workspaces>/.mash/uids.json` (root only, on the volume), and a folder
  owned by that uid with mode 0700. The top folder is root's, mode 0711, so
  no agent can create, claim or list another's folder, read another's
  processes' environment, or plant files such as `sitecustomize.py` where
  another agent's code loads them. When the daemon starts it adds an
  `agent-<uid>` passwd entry for each mapped uid; a folder still owned by an
  older shared uid is handed over on first use, without following links or
  entering bind mounts, whose files keep their owner on the Docker host.
  Without root, as on a developer's machine, every agent runs as the current
  user.
- `tokens.py`: `derive_sandbox_token(secret, policy_id)`, the HMAC of
  `mash-sandbox:<policy_id>` under `MASH_HOST_SECRET`. Only the host can derive
  it, and one policy's token opens no other sandbox. `derive_link_token` and
  `policy_for_link_token` make and read the `<base64url(policy_id)>.<hmac>`
  tokens in links.
- `provider.py`: the `SandboxProvider` protocol (`ensure`, `stop`, `delete`,
  `list`, `close`) and `DockerSandboxProvider`: container and volume
  `<ns>-sandbox-<key>` (`<key>` is 16 hex characters of the sha256 of
  `policy:<policy_id>`, since Docker names allow no `@` or `:`), labels
  `mash.sandbox.policy` and
  `mash.namespace`, restart policy `unless-stopped`. Each running sandbox has
  a network of the same name with a /28 from `MASH_SANDBOX_SUBNETS` (default
  `10.213.0.0/16`); `stop_sandbox` disconnects everything and removes it, and
  `prepare` removes those of sandboxes that are not running. A container made
  for another network is replaced like one made from another image.
  Sandboxes run `ghcr.io/imsid/mash-sandbox:<mashpy version>`
  (`Dockerfile.sandbox`), pulled on first start, or `MASH_SANDBOX_IMAGE`. A
  container whose `Config.Image` is another image, as after a mashpy upgrade,
  is removed and created again; the policy's files stay in the volume.
  `delete` removes the container, its network and its volume, for
  `DELETE /api/v1/policies/{policy_id}`. A
  host in a container joins each running sandbox's network and reaches daemons by name; a host on
  the Docker host publishes each daemon's port on `127.0.0.1`. `MASH_SANDBOX_MOUNTS`
  adds `source:target[:ro]` bind mounts; `MASH_SANDBOX_NAMESPACE` replaces
  `mash` in the names.
- `manager.py`: `SandboxManager`, the host's view, and the one way into a
  sandbox: `exec`, `read_file`, `write_file` and `proxy`. It starts a policy's
  sandbox under a per-policy lock on first use, stops one with nothing in flight
  and no use for `MASH_SANDBOX_IDLE_SECONDS` (default 900), and keeps at most
  `MASH_SANDBOX_MAX_RUNNING` (default 20) running, stopping the least recently
  used idle one to make room or raising `SandboxCapacityError` when all are
  busy. The stop runs outside the lock that picks it, so other policies' starts
  don't wait behind it; the stopping sandbox counts against the cap until it
  is stopped, and a caller that finds every idle sandbox already stopping
  waits for one. After a host restart, running sandboxes count as active from
  then.
- `containers.py`: `ContainerManager`, that lifecycle on its own, shared with
  the browsers' `BrowserManager` (`mash.browser`). `hold(policy_id)` keeps a
  container counted as in use for the length of a block.
- `docker_engine.py`: the minimal async Docker Engine API client over the Unix
  socket (`DOCKER_HOST`, `/var/run/docker.sock`, or Docker Desktop's
  `~/.docker/run/docker.sock`).

`mash.testing.sandboxes.LocalSandboxProvider` runs daemons in-process on
localhost ports with a temporary directory per policy, for tests.
