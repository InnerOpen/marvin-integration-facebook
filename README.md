# marvin-integration-facebook

A [Marvin](https://github.com/InnerOpen/marvin) integration that posts to a **Facebook Page** through the Graph API:
text, a link, one photo, or several photos. It is the Facebook side of Marvin's shared **`social.post`** capability
(a workflow step says "post this" and every connected network posts it), and it works as a plain action too.
Category: *destination*. Its sibling for the other Meta network is `marvin-integration-instagram`.

## Install

On a Marvin host:

```bash
uv pip install marvin-integration-facebook   # or add to your Marvin image
# restart Marvin → "Facebook Page" appears in Settings → Integrations
```

No changes to Marvin core or its frontend. Marvin discovers the provider through the `marvin.integrations` entry point
this package declares.

## Set up the Meta app and a Page token

Posting as a Page needs a **Page access token**. The steps below use the Graph API; nothing here is secret until you
paste a real value, and none of it belongs in a repo.

1. **The app.** In the [Meta App Dashboard](https://developers.facebook.com/apps/) use the Business app you already have for
   Instagram (one app can serve both), or create one. Add the use case or product that grants Page permissions
   (**Manage everything on your Page** / Facebook Login). *Verify:* Meta renames these screens often; what matters is that the
   app can request the three permissions below.
2. **Permissions** the user token must carry:
   - `pages_manage_posts`: create the posts.
   - `pages_read_engagement`: read the post back for its permalink, and `list_recent_posts`.
   - `pages_show_list`: list the Pages you manage, to find the Page ID and token.
3. **Access level.** While the app is in **Development** mode these permissions work for people who hold a role on the app
   (admin, developer, tester), on Pages they manage: enough for your own Page, with **no App Review**. Posting for Pages
   owned by other people needs *Advanced Access* (App Review and Business Verification). Add yourself as a tester if the
   app is not yours. You must also be able to manage the Page itself, with full control, or the Page token will not be
   allowed to post. *Verify:* Meta's wording for the required Page task (`CREATE_CONTENT`) has changed with the new Pages experience.
4. **A long-lived Page token**, using the Graph API Explorer (select your app, "Get User Access Token", tick the three
   permissions) or your own OAuth flow:
   ```bash
   # 1. Exchange the short-lived user token for a long-lived (about 60 days) user token.
   curl -G "https://graph.facebook.com/v22.0/oauth/access_token" \
     -d grant_type=fb_exchange_token -d client_id=$APP_ID -d client_secret=$APP_SECRET \
     -d fb_exchange_token=$SHORT_LIVED_USER_TOKEN

   # 2. List your Pages. Each row carries the Page's id and its own access_token.
   curl -G "https://graph.facebook.com/v22.0/me/accounts" -d access_token=$LONG_LIVED_USER_TOKEN
   ```
   A Page token obtained from a **long-lived user token** does not expire (*verify* in the token debugger: `GET
   /debug_token?input_token=…` should show `expires_at: 0`). Take the Page's `id` (the **Page ID**) and its `access_token`.
5. **In Marvin:** **Settings → Integrations → Facebook Page → Configure**: paste the Page token as **Page access token**,
   set **Page ID**, save. The card should turn `ok`: Marvin reads the Page with the token and checks that the id matches.
   Keep `$APP_SECRET` and the tokens in a password manager or shell variables; never in notes or commits.

## Credential, config & actions

| | |
|---|---|
| **Credential** | `page_access_token`: the long-lived Page token, sent as `Authorization: Bearer` (never in a URL) |
| **Config** | `page_id` (required, numeric), `api_version` (`v22.0`) |
| **Health** | `GET /{page_id}?fields=id,name` with the token; an id that differs from `page_id` is an error |
| **Action** | `post`: the `social.post` capability, approval-gated (below) |
| **Action** | `list_recent_posts`: `{ "limit"?: 1–100 (10) }` → `{ "posts": [{ post_id, message, created_time, url }] }`. A read; no approval |

### `post`: the shared `social.post` capability

| `social.post` arg | Facebook |
|---|---|
| `text` (required) | `message` of a feed post, or the `caption` of a photo post |
| `images` (0–10 public https URLs) | none: a feed post. One: a photo post (`POST /{page}/photos`, `url`, `caption`, `published=true`). Several: each photo uploaded with `published=false`, then one `POST /{page}/feed` with `message` and `attached_media[n]={"media_fbid":…}` |
| `alt` (one per image) | `alt_text_custom` on each photo (up to 1,000 characters) |
| `link` | no images: the feed post's `link` (Facebook shows a preview). With images: appended to the text on its own line, since a photo post can't carry a link attachment (Facebook makes a URL in text clickable) |
| `dry_run` | reports `would_post` and calls nothing. Default **false**: the approval step before the post is the gate |

Output: `{ "posted": true, "post_id", "url", "network": "facebook", "kind": "text|photo|photos", "images"? }`. A dry run
returns `{ "posted": false, "dry_run": true, "post_id": null, "url": null, "network": "facebook", "would_post": { "kind", "endpoint", "message"|"caption", "link"?, "images"?, "alt"? } }`.
`url` is the post's permalink; if Facebook won't give it, it is `null` (the post is still out and not reported as a failure).

What Facebook (or the contract) can't do is refused **before any call** with an `IntegrationError` (code `unsupported`,
still a `ValueError`): empty text, more than 10 images, a non-https image URL, more `alt` entries than images, a `link`
that isn't a URL. Failed calls raise an `IntegrationError` carrying Facebook's message and one of the codes
`auth_expired` (190), `permission_denied` (10, 200–299), `rate_limited` (4, 17, 32, 341, 613), `blocked` (368),
`invalid_request` (100) or `unknown`. No error policy is declared, so nothing is retried; if a multi-photo post fails
after some photos were uploaded, those stay unpublished on the Page and are never shown.

### Verify against Meta's current docs

I wrote this against the Graph API as I know it; these are the points that vary by version or that I could not check
against a live Page. Test each once with a real token (a text post first):

- `alt_text_custom` as the photo alt-text parameter on `/{page}/photos`.
- `attached_media[n]` as a JSON string with `media_fbid`, together with `published=false` photos (some docs also add
  `temporary=true` for photos that will never be posted on their own; this plugin does not send it).
- A published photo's response including `post_id` (used for the permalink); older responses carry only `id`.
- `permalink_url` on a Page post: it has come back without the host (`/123/posts/456`); the plugin adds
  `https://www.facebook.com`.
- `GET /{page}/posts` (used by `list_recent_posts`) versus `/published_posts`; and the `pages_read_engagement` need.
- The default `api_version` `v22.0` is Instagram's; Meta retires versions after about two years, so bump it in the
  config when Meta says so.
- The Graph error-code mapping above, which comes from Meta's error reference.

## What it teaches Marvin

A guide (`src/marvin_integration_facebook/guide.md`) that Marvin's agents read when building workflows where this Page is
connected. There is **no recipe**: the right shape is the `social.post` capability step after an Approval step, which
Marvin core provides.

## Logo

None is shipped: the Facebook mark is Meta's to supply and must not be altered. Download it from Meta's brand resources,
drop it beside `provider.py` as `logo.png` or `logo.svg` (at most 64 KB), and set `logo = "logo.png"` on the provider.
Until then Marvin shows the 📘 emoji.

## Develop

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
```

The provider depends only on `marvin-integration-sdk`, not on Marvin core, so tests run standalone against a fake http
helper. Tests use fake tokens only.
