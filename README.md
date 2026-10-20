# marvin-integration-facebook

A [Marvin](https://github.com/InnerOpen/marvin) integration that posts to a **Facebook Page** through the Graph API:
text, a link, one photo, or several photos. It is the Facebook side of Marvin's shared **`social.post`** capability
(a workflow step says "post this" and every connected network posts it), and it works as a plain action too. Since 0.2.0
it also reads the Page's comments and answers them, through Marvin's shared engagement capabilities (`social.comments`,
`social.reply`, `social.like`, `social.hide`, `social.delete_comment`, `social.message`), which the core auto-reply
engine and workflows use. Category: *destination*. Its sibling for the other Meta network is `marvin-integration-instagram`.

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
   app can request the permissions below.
2. **Permissions** the user token must carry:
   - `pages_manage_posts`: create the posts.
   - `pages_read_engagement`: read the post back for its permalink, `list_recent_posts`, and the Page's posts and comments.
   - `pages_show_list`: list the Pages you manage, to find the Page ID and token.
   - For comments and replies (0.2.0; only needed for those actions):
     - `pages_read_user_content`: read the comments (and who wrote them) on the Page's posts. *Verify:* Meta has moved
       comment reads between this and `pages_read_engagement`; ask for both.
     - `pages_manage_engagement`: reply publicly, like, hide and delete comments.
     - `pages_messaging`: private replies and `message` (Messenger). This one usually needs **Advanced Access** (App Review)
       for anyone who is not a role-holder on the app, and the Page must have Messenger enabled. *Verify.*
3. **Access level.** While the app is in **Development** mode these permissions work for people who hold a role on the app
   (admin, developer, tester), on Pages they manage: enough for your own Page, with **no App Review**. Posting for Pages
   owned by other people needs *Advanced Access* (App Review and Business Verification). Add yourself as a tester if the
   app is not yours. You must also be able to manage the Page itself, with full control, or the Page token will not be
   allowed to post. *Verify:* Meta's wording for the required Page task (`CREATE_CONTENT`) has changed with the new Pages experience.
4. **A long-lived Page token**, using the Graph API Explorer (select your app, "Get User Access Token", tick the
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
| **Config** | `page_id` (required, numeric), `api_version` (`v22.0`), `lookback_posts` (10: recent posts scanned for comments) |
| **Health** | `GET /{page_id}?fields=id,name` with the token; an id that differs from `page_id` is an error |
| **Action** | `post`: the `social.post` capability, approval-gated (below) |
| **Action** | `list_recent_posts`: `{ "limit"?: 1–100 (10) }` → `{ "posts": [{ post_id, message, created_time, url }] }`. A read; no approval |
| **Actions** | `comments`, `reply`, `like`, `hide`, `delete_comment`, `message`: the shared engagement capabilities (below) |

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

### Shared engagement capabilities (0.2.0)

Each is declared on one action. **Gated** means `requires_approval`: a workflow may run it only after an approved Approval step
(a published auto-reply rule is the approval for the engine's own replies). The Instagram (0.7.0) and Mastodon (0.2.0) plugins
declare the same shapes.

| Action | Capability | Gated | Graph call |
|---|---|---|---|
| `comments` | `social.comments` | no | `GET /{page}/posts?fields=id,created_time,permalink_url&limit={lookback_posts}`, then per post `GET /{post}/comments?filter=stream&order=reverse_chronological&fields=id,message,created_time,from{id,name},permalink_url,parent{id}&limit=50` |
| `reply` | `social.reply` | yes | private (default): `POST /{page}/messages` JSON `{recipient:{comment_id}, message:{text}}`, after `GET /{comment}?fields=id,created_time,from`. Public: `POST /{comment}/comments` `message=…` |
| `like` | `social.like` | no | `POST /{comment}/likes` |
| `hide` | `social.hide` | no | `POST /{comment}?is_hidden=true` (or `false`) |
| `delete_comment` | `social.delete_comment` | yes | `DELETE /{comment}` |
| `message` | `social.message` | yes | `POST /{page}/messages` JSON `{recipient:{id}, messaging_type:"RESPONSE", message:{text}}` |

There is no `social.repost`. The token is always the `Authorization: Bearer` header.

**`comments`**: `{ since?, limit? (50), kinds? }` → `{ items, cursor }`, oldest to newest. An item is `{ id, kind: comment|reply,
post_id, post_url, author: {id, handle, name}, text, created_at (ISO, UTC), url, network: "facebook" }`. With `filter=stream`
Facebook returns a post's comments and their replies in one flat list; a row with a `parent` is a `reply`, the rest are
`comment`. The newest 50 per post are read, so a post with more than 50 new comments between polls loses the oldest. The
cursor is opaque (the newest `created_at` returned plus the ids sharing that second), so nothing is repeated or skipped; a
first poll without `since` returns the newest `limit` items, and a poll with one drains the backlog oldest first. A bad
cursor raises `invalid_cursor`. `author.handle` and `author.name` are both the commenter's name (the Pages API has no
username); `author.id` is the page-scoped id, which is what `message` takes, and may be empty when Facebook withholds the
commenter. `mention` returns nothing (Facebook tells a Page about tags only by webhook), and asking for mentions alone is
refused as `unsupported`.

**`reply`**: `{ comment_id, text, private = true, dry_run = false }` → `{ replied, reply_id, private, url, network }` (a dry
run adds `dry_run: true` and `would_reply`, and sends nothing). A private reply is one Messenger message per comment, within 7
days of the comment, which the plugin checks first (`unsupported` for an older comment or the Page's own); Facebook rejects a
second private reply to the same comment. Public replies return the new comment's permalink in `url` when Facebook gives it.

**`message`**: `{ to: {id?, handle?}, text, dry_run = false }` → `{ sent, message_id, network }`. `to.id` is a page-scoped id.
Facebook allows a message only within **24 hours of the person's last message to the Page**. That time is not available to
the plugin, so it does not check it: a late send fails at Facebook, and the error with subcode 2018278 is mapped to
`unsupported` (other Messenger refusals keep the generic mapping). A handle alone can't be resolved to an id and is refused
as `unsupported`.

Refusals are `IntegrationError` with code `unsupported` (Messenger text over 2,000 characters, an unknown kind, a private
reply outside its window, a message to a handle). A missing `comment_id`, `text` or `to` is a plain `ValueError`, like Instagram.

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
- Engagement (0.2.0), none of it run against a live Page:
  - which permission gates comment reads: `pages_read_user_content` and `pages_read_engagement` are both listed because Meta's
    docs have named each; and whether `from` is returned for every commenter (it is withheld without them).
  - `filter=stream` with `order=reverse_chronological` on `/{post}/comments`, and `parent{id}` being present on replies only.
  - `/{page}/posts` (used here, includes visitor posts) versus `/published_posts` (the Page's own posts only).
  - A private reply's request shape (`recipient.comment_id`, no `messaging_type`), the 7-day window and the one-reply-per-comment
    rule, and the error Facebook gives for a second one (not mapped specially; it surfaces with Facebook's own message).
  - `POST /{comment}?is_hidden=` as the hide call, and `POST /{comment}/likes` liking as the Page.
  - Error subcode 2018278 (message outside the 24-hour window) and whether Facebook uses other subcodes for the same case;
    Messenger's 2,000-character text limit.
  - Replying publicly to a reply (`POST /{reply}/comments`): Facebook keeps only two levels, so it may attach to the parent
    or be refused.
  - Whether Messenger needs Advanced Access / App Review for private replies to people without a role on the app.

## Workflow Library recipe

**Post to Facebook when published** (`post-on-publish`): when an entry of the chosen type is published, an editor
is asked first; once approved, its title, summary and link go to Facebook, and the post's id and link are kept on the entry
(`facebook_post_id`, `facebook_url`).

## What it teaches Marvin

A guide (`src/marvin_integration_facebook/guide.md`) that Marvin's agents read when building workflows where this Page is
connected, including the engagement actions. There is **no recipe**: the right shape is the `social.post` (or `social.reply`,
`social.message`, `social.delete_comment`) capability step after an Approval step, which Marvin core provides; reading, liking
and hiding need none.

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
