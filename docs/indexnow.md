# Search engine URL submissions

The release supports [IndexNow](https://www.indexnow.org/documentation) without a Bing Webmaster account. It supplements the public sitemap and does not replace Google Search Console.

Set `INDEXNOW_KEY` in the production environment to a random value of 8–128 ASCII letters, digits, or hyphens. The application serves that value as plain text at `/indexnow-key.txt`; the route returns 404 when unset or malformed. Keep the value out of Git and command output. This proof authorizes URL notifications only and grants no application/account access.

Preview the canonical production sitemap:

```sh
python scripts/submit_indexnow.py
```

After deployment, run the following with the same `INDEXNOW_KEY` securely supplied in the process environment:

```sh
python scripts/submit_indexnow.py --submit
```

The command checks the live proof before making a submission. It rejects redirects, foreign hosts, non-HTTPS URLs, query strings and oversized XML. It filters operational, authenticated connector and paid API routes. It submits only the remaining sitemap URLs to `api.indexnow.org` and reports the response without the key. HTTP 200 means received; 202 means key validation is pending. Neither confirms indexing or rankings. Submit after substantive public content changes rather than on a timer.

Google Search Console and DNS-AID setup remain deferred at the owner's request. The separate Auth.md registration flow awaits an access-policy decision; existing Clerk connector authentication remains the live sign-in method.
