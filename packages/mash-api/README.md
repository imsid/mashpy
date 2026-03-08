# mash-api

OpenAPI service package for self-hosted Mash applications.

## Install

```bash
pip install mash-api
# or
pip install "mashpy[api]"
```

## Usage

```python
from mash_api import create_app
from my_app import definition

app = create_app(definition)
```

Run with Uvicorn:

```bash
uvicorn my_module:app --host 127.0.0.1 --port 8000
```

Or use the bundled CLI:

```bash
mash-api --app my_app:build_definition
```
