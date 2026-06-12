# RTT Server

Flask app exposing the RTT CLI as an MCP server (for Claude) and a REST API (for Custom GPT), deployable to AWS Lambda via Zappa.

## Endpoints

| Endpoint | Description |
|---|---|
| `POST /mcp` | MCP Streamable HTTP — tools for Claude |
| `GET /api/trains` | Departure board |
| `GET /api/service` | Service calling points |
| `GET /api/route` | Two-leg connection finder |
| `GET /openapi.json` | OpenAPI spec for Custom GPT |

## Local development

```bash
cd /path/to/rttcli
python -m venv .venv-server
source .venv-server/bin/activate
pip install -r server/requirements.txt

export RTT_TOKEN=your_token_here
python server/app.py
```

The server runs on `http://localhost:5000`. Test it:

```bash
curl "http://localhost:5000/api/trains?from=PAD&to=BRI"
curl "http://localhost:5000/api/route?from1=BRI&to1=PAD&from2=KGX&to2=CMB"
```

## Deploy to AWS Lambda (Zappa)

### Prerequisites

- AWS CLI configured (`aws configure`) with permissions to create Lambda functions, API Gateway, and S3 buckets
- An S3 bucket in `eu-west-2` to store deployment packages (or update `s3_bucket` in `zappa_settings.json`)

### First deploy

```bash
# From the repo root
source .venv-server/bin/activate

# Edit zappa_settings.json — set your RTT_TOKEN and s3_bucket
# Then deploy:
zappa deploy dev
```

Zappa will print an API Gateway URL like:
```
https://abc123xyz.execute-api.eu-west-2.amazonaws.com/dev
```

### Update after code changes

```bash
zappa update dev
```

### View logs

```bash
zappa tail dev
```

### Tear down

```bash
zappa undeploy dev
```

## After deploying

Update the `servers.url` in `server/app.py` (`_OPENAPI` dict) to your API Gateway URL, then redeploy. This URL is what Custom GPT reads from `/openapi.json`.

## Connecting to Claude (MCP)

Add to your Claude MCP config (`~/.claude/mcp.json` or via Claude Desktop settings):

```json
{
  "mcpServers": {
    "rtt": {
      "url": "https://YOUR_API_GATEWAY_URL/dev/mcp",
      "transport": "http"
    }
  }
}
```

Claude will then have access to three tools: `search_trains`, `get_service_detail`, and `search_route`.

## Connecting to ChatGPT (Custom GPT)

1. Go to [chat.openai.com](https://chat.openai.com) → **Explore GPTs** → **Create**
2. In the **Configure** tab, scroll to **Actions** → **Create new action**
3. In the schema field, paste the URL: `https://YOUR_API_GATEWAY_URL/dev/openapi.json`
   (ChatGPT will import the spec automatically)
4. Set **Authentication** to **None**
5. Save the GPT

The GPT will now be able to search trains, look up calling points, and find connections.

## Available query parameters

### `/api/trains`
| Parameter | Required | Description |
|---|---|---|
| `from` | ✓ | Origin CRS code (e.g. `PAD`) |
| `to` | ✓ | Destination CRS code |
| `date` | | Date `YYYY-MM-DD` (default: today) |
| `after` | | Departing after `HHMM` |
| `arriveby` | | Arriving by `HHMM` |

### `/api/service`
| Parameter | Required | Description |
|---|---|---|
| `identity` | ✓ | Service identity from `searchTrains` result |
| `date` | ✓ | Date `YYYY-MM-DD` |
| `from` | | Boarding station CRS (highlights stop) |
| `to` | | Alighting station CRS (highlights stop) |

### `/api/route`
| Parameter | Required | Description |
|---|---|---|
| `from1` | ✓ | Leg 1 origin CRS |
| `to1` | ✓ | Leg 1 destination / interchange CRS |
| `from2` | ✓ | Leg 2 origin CRS |
| `to2` | ✓ | Leg 2 destination CRS |
| `transfer` | | Minimum transfer minutes (default: `25`) |
| `date` | | Date `YYYY-MM-DD` |
| `after` | | After `HHMM` |
| `arriveby` | | Arriving at final destination by `HHMM` |
