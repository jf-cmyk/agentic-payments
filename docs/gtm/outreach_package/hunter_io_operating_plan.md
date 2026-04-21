# Hunter.io Operating Plan

## Role In The GTM Motion

Hunter.io should be used for:

- Domain Search.
- Email Finder when we know a specific person.
- Email Verifier before sending.
- Optional lead list / sequence management after Johann approves.

Hunter.io should not be used for:

- Unapproved bulk sending.
- Uploading all 25 targets before message-market fit is proven.
- Sending from an unreviewed sequence.
- Storing sensitive Blocksize secrets beyond the Hunter API key itself.

## API Key Handling

When Johann provides the API key:

- Do not print it.
- Use it only with `https://api.hunter.io/v2/*`.
- Store it only if Johann explicitly asks. Preferred first pass: use it as an environment variable for one run.
- Redact it from logs, package files, and terminal output.

Suggested local environment variable:

```bash
HUNTER_API_KEY=...
```

## Recommended API Calls

1. Account check, free:

```text
GET https://api.hunter.io/v2/account?api_key=$HUNTER_API_KEY
```

2. Domain Search, one domain at a time:

```text
GET https://api.hunter.io/v2/domain-search?domain=dune.com&api_key=$HUNTER_API_KEY
```

3. Email Finder, when a target person is known:

```text
GET https://api.hunter.io/v2/email-finder?domain=example.com&first_name=First&last_name=Last&api_key=$HUNTER_API_KEY
```

4. Email Verifier, before any send:

```text
GET https://api.hunter.io/v2/email-verifier?email=name@example.com&api_key=$HUNTER_API_KEY
```

## Credit Rules From Hunter Docs

- Domain Search: commonly 1 credit for 1 to 10 emails returned per domain.
- Email Finder: commonly 1 credit per call when an email is found.
- Email Verifier: commonly 0.5 credit per verification.
- Account information is free.
- API calls are one item at a time; Hunter does not offer bulk API calls in the documented flow.

## Rate Limits From Hunter Docs

- Domain Search and Email Finder: 15 requests/second and 500 requests/minute.
- Email Verifier: 10 requests/second and 300 requests/minute.

The sprint should stay far below these limits.

## Contact Selection Rules

Prefer contacts with one of these functions:

- Partnerships.
- Ecosystem.
- Developer relations.
- Product.
- API/platform.
- Data platform.
- Founder/CEO/CTO for smaller companies.

Avoid:

- Generic support inboxes unless no better path exists.
- Recruiting, finance, legal, unrelated sales roles.
- Contacts with low confidence or invalid verification.

## First API Run Plan

After Johann provides the API key:

1. Check account endpoint and credit availability.
2. Run Domain Search for the first five domains:
   - `dune.com`
   - `browserbase.com`
   - `firecrawl.dev`
   - `exa.ai`
   - `coinbase.com`
3. Produce `hunter_leads_review.csv` with:
   - account
   - domain
   - contact name
   - role/title
   - email
   - Hunter confidence
   - verification status
   - recommended message variant
   - approval status
4. Ask Johann to approve exact recipients before any send.

## Sending Route

Best route:

- Use Hunter to discover and verify leads.
- Use Hunter Sequences only if Johann has a connected sending account and approves a specific sequence.
- Otherwise send manually from Johann's preferred email account after approval.

Calendar invites should be created only after a positive reply, using the calendar plugin, with `jf@blocksize-capital.com` included.

## Sources

- Hunter API overview: https://help.hunter.io/en/articles/1970956-hunter-api
- Hunter API reference v2: https://hunter.io/api-documentation/v2
- Hunter Domain Search API: https://hunter.io/api/domain-search
- Hunter Email Finder API: https://hunter.io/api/email-finder
- Hunter Sequences API: https://hunter.io/api/campaigns
- Hunter email sending overview: https://help.hunter.io/en/articles/2933755-can-i-send-emails-with-hunter
