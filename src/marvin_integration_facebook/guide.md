# Facebook Page

Posts to one Facebook Page through the Graph API with a Page access token: text, a link, or photos. It is Marvin's
Facebook side of the shared **social post** (capability `social.post`), so a workflow step that says "post this" can
reach Facebook next to the other connected networks. It also reads the Page's comments and answers them (the shared
engagement capabilities below). It cannot post to a personal profile or a group, schedule a post, or post video.

## Connect it

1. In the Meta App Dashboard (developers.facebook.com) use a Business app (the one Instagram uses works) and get a
   **long-lived Page access token** with `pages_manage_posts`, `pages_read_engagement` and `pages_show_list`; for the
   engagement actions also `pages_read_user_content` (read comments), `pages_manage_engagement` (reply, like, hide,
   delete) and `pages_messaging` (private replies and messages). Note the Page's numeric **Page ID**. The README
   walks through it.
2. In Marvin: **Settings → Integrations → Facebook Page → Configure**. Paste the token as the **Page access token**, set
   **Page ID**. Save: the card should turn `ok` (Marvin reads the Page with the token and checks the id matches).
3. Optional: **Graph API version** (default `v22.0`) and **Posts to scan** (how many recent posts to check for comments,
   default 10).

## Actions

- **Post to the Facebook Page** (`post`, capability `social.post`): `text` (required), `images` (0–10 public https image
  URLs), `alt` (one alt text per image), `link`, `dry_run` (default `false`: the approval step before it is the gate).
  - No images: a feed post with the text, and the `link` attached (Facebook shows a link preview).
  - One image: a photo post; the text is the caption. A photo post cannot carry a link preview, so the `link` is added
    to the text on its own line.
  - Several images: every photo is uploaded unpublished, then one feed post carries the text and all of them.
  - Returns `posted`, `post_id`, `url` (the permalink, or null if Facebook withholds it), `network` (`facebook`), `kind`
    (`text`, `photo` or `photos`) and `images`. A dry run returns `posted: false` and `would_post` (what would be
    sent) and calls nothing.
  - Refused with a clear error before any call: empty text, more than 10 images, an image URL that is not https, more
    alt texts than images, a link that is not a URL.
- **List recent posts** (`list_recent_posts`): `limit` (1–100, default 10). Returns `{"posts": [{post_id, message,
  created_time, url}]}`. A read, so it needs no approval; useful to check that a post landed.

### Comments and replies (the shared engagement capabilities)

Marvin's auto-reply engine and workflow steps use these by capability name. All of them are about the Page's own posts.

- **Read comments and replies** (`comments`, `social.comments`, no approval): `since` (the opaque cursor this action
  returned last time), `limit` (default 50), `kinds` (`comment`, `reply`; `mention` returns nothing). Returns `{items,
  cursor}`, oldest first; each item has `id`, `kind`, `post_id`, `post_url`, `author` (`id`, `handle`, `name`), `text`,
  `created_at` (UTC), `url`, `network`. The Pages API gives no username, so `author.handle` is the commenter's name;
  `author.id` (and the name) can be empty when Facebook withholds the commenter. It reads the latest posts (the **Posts to
  scan** setting) and, per post, the newest 50 comments and replies.
- **Reply to a comment** (`reply`, `social.reply`, **needs approval**): `comment_id`, `text`, `private` (default true),
  `dry_run`. A **private** reply is a Messenger message to the commenter (needs `pages_messaging`); Facebook allows one
  per comment and only within 7 days of the comment, so an older comment is refused as `unsupported` (as is the Page's
  own comment). `private: false` replies in the thread. Returns `replied`, `reply_id`, `private`, `url` (public replies
  only), `network`; a dry run returns `would_reply` and sends nothing.
- **Like a comment** (`like`, `social.like`, no approval): `comment_id`; likes it as the Page.
- **Hide or unhide a comment** (`hide`, `social.hide`, no approval): `comment_id`, `hidden` (default true).
- **Delete a comment** (`delete_comment`, `social.delete_comment`, **needs approval**): `comment_id`; permanent.
- **Send a Messenger message** (`message`, `social.message`, **needs approval**): `to` (`{id}`: the person's page-scoped
  id, which is the `author.id` of a comment), `text`, `dry_run`. Facebook allows it only within 24 hours of the person's
  last message to the Page; the plugin can't see that time, so Facebook decides and a late send fails with
  `unsupported`. A name or handle alone is refused (`unsupported`): it can't be turned into an id. Returns `sent`,
  `message_id`, `network`.

There is no `social.repost` on Facebook.

## In workflows

**Post to the Facebook Page** requires approval: a workflow may run it only right after an **Approval** step that was
approved. A step may name the capability `social.post` instead of this action. The same holds for **Reply to a comment**,
**Delete a comment** and **Send a Messenger message**; reading, liking and hiding need no approval. Images must be public https URLs that
Facebook can fetch (an asset's public URL works); Facebook accepts JPEG, PNG, GIF, BMP and TIFF.

## What it can't do

- Post as a person, to a group or an event, schedule for later (use a workflow wait), or post video or a story.
- Edit or delete a post.
- Read Page tags or mentions (Facebook only sends those by webhook), or message someone who has not written to the Page in
  the last 24 hours.
- Take more than 10 images in one post (Marvin's shared limit).

## When it fails

Facebook's own message is passed through, with its error code. The failure carries a code Marvin can read
(`${error.code}`); nothing is retried automatically.

- **"No Facebook Page access token configured."**: the credential is empty.
- **`auth_expired`** (Graph code 190): the token is invalid or was revoked; make a new Page token.
- **`permission_denied`** (codes 10, 200–299): the token lacks a permission (`pages_manage_posts` to post, or one of the
  engagement permissions above), the Page role was lost, or the app is not allowed to act (in Development mode only people
  with a role on the app can be reached).
- **`rate_limited`** (codes 4, 17, 32, 341, 613): too many calls; wait.
- **`blocked`** (code 368): Facebook blocked the post as spam or policy; nothing was posted.
- **`unsupported`**: this plugin refused the request before calling Facebook (see the list under *Post*; a private reply
  to a comment older than 7 days, a message to a handle, an unknown comment kind), or Facebook said a message is outside
  its 24-hour window. Nothing is retried.
- **`invalid_cursor`**: `since` is not a cursor this plugin returned; read again without it.
- The card shows **unconfigured** when the token or Page ID is missing, and **error** when the Page can't be read or the
  token belongs to a different Page.
