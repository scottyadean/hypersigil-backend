# Hypersigil API

Serverless Python API for posting and reading short intentions. Runs on AWS
Lambda behind API Gateway, backed by MongoDB Atlas.

The frontend lives in its own repo: [hypersigil-frontend](https://github.com/scottyadean/hypersigil-frontend).

## Layout

```
src/thoughs.py        lambda handlers
src/utils.py          mongo client, response helpers, validation, passcode hashing
tests/                handler tests against an in-memory mongo
config/local.yml      local stage config (gitignored - holds the real connection string)
config/prod.yml       prod stage config (DB_URL resolved from SSM at deploy time)
server.sh             runs serverless offline on port 5500
serverless.yml        routes, iam, packaging
```

## API

| Method | Path                        | API key | Notes                                         |
| ------ | --------------------------- | ------- | --------------------------------------------- |
| GET    | `/list/thoughts`            | no      | newest first, `?limit=` (default 100, max 1000) |
| GET    | `/get/thought/{thought_id}` | yes     | single thought                                |
| POST   | `/create/thought`           | yes     | `{"name", "thought", "passcode"}`             |
| PUT    | `/edit/thought/{thought_id}` | yes    | `{"thought", "passcode"}`, optional `"name"`  |
| POST   | `/flag/thought/{thought_id}` | yes    | increments the flag count                     |

The feed is deliberately open so the page can render without a key. Everything
that writes requires the `x-api-key` header.

`name` is capped at 255 characters, `thought` at 1500.

### Thought shape

```json
{
  "id": "6a6a85c566d18222455aea83",
  "name": "scott",
  "thought": "the text",
  "created_at": "2026-07-29T22:59:17.390000+00:00",
  "updated_at": null,
  "flags": 0,
  "editable": true
}
```

`editable` reports whether a passcode was set. The passcode and its hash are
never returned by any endpoint.

## Editing

A thought may carry a passcode, set at creation. Supplying it to
`/edit/thought/{id}` allows the text (and optionally the name) to be rewritten.

Passcodes are stored as PBKDF2-SHA256, 200,000 iterations, with a 16-byte
random salt per thought — never in plaintext, and never recoverable. Comparison
is constant time. Omitting the passcode at creation is allowed; the thought
simply cannot be edited afterwards.

## Flagging

Each flag increments a counter atomically. Once a thought reaches
`FLAG_THRESHOLD` (20) it disappears from `/list/thoughts` and returns 404 from
`/get/thought/{id}` and `/edit/thought/{id}`, so a removed thought cannot be
read by direct link or edited back into circulation.

There is no per-person flag tracking. Twenty flags means twenty requests, not
twenty people.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install mongomock pytest     # tests only

.venv/bin/python -m pytest tests/          # no database required
npm install
./server.sh                                # serverless offline on :5500
```

`server.sh` reads `config/local.yml`, which is gitignored and holds the real
connection string. Create it from `config/prod.yml` and substitute a literal
`DB_URL`.

## Deploying

`DB_URL` for prod comes from SSM, so the parameter must exist first:

```bash
aws ssm put-parameter \
  --name /hypersigil/prod/db_url \
  --type SecureString \
  --value 'mongodb+srv://...'

npm install
npx serverless deploy --stage prod
```

Deploying prints the endpoint and provisions an API key named
`prod-hypersigil-api-key`. Both go into the frontend's `js/config.js`.

## Notes

`ALLOWED_ORIGIN` is `"*"` in `config/prod.yml`. Narrow it to the site origin
once the frontend URL is settled.

`list_thoughts` sorts on `created_at`. Once the collection grows past a few
thousand documents, add an index:

```js
db.thoughts.createIndex({ created_at: -1, _id: -1 })
```

Never commit a real connection string. `config/local.yml` is gitignored and
`config/prod.yml` must keep the `${ssm:...}` reference rather than a literal.
