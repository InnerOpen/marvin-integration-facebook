# Facebook Page

Posts to one Facebook Page through the Graph API with a Page access token: text, a link, or photos. It is Marvin's
Facebook side of the shared **social post** (capability `social.post`), so a workflow step that says "post this" can
reach Facebook next to the other connected networks. It cannot post to a personal profile or a group, schedule a post,
post video, or read comments.

## Connect it

1. In the Meta App Dashboard (developers.facebook.com) use a Business app (the one Instagram uses works) and get a
   **long-lived Page access token** with `pages_manage_posts`, `pages_read_engagement` and `pages_show_list`. Note the
   Page's numeric **Page ID**. The README walks through it.
2. In Marvin: **Settings → Integrations → Facebook Page → Configure**. Paste the token as the **Page access token**, set
   **Page ID**. Save: the card should turn `ok` (Marvin reads the Page with the token and checks the id matches).
3. Optional: **Graph API version** (default `v22.0`).

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

## In workflows

**Post to the Facebook Page** requires approval: a workflow may run it only right after an **Approval** step that was
approved. A step may name the capability `social.post` instead of this action. Images must be public https URLs that
Facebook can fetch (an asset's public URL works); Facebook accepts JPEG, PNG, GIF, BMP and TIFF.

## What it can't do

- Post as a person, to a group or an event, schedule for later (use a workflow wait), or post video or a story.
- Edit or delete a post.
- Take more than 10 images in one post (Marvin's shared limit).

## When it fails

Facebook's own message is passed through, with its error code. The failure carries a code Marvin can read
(`${error.code}`); nothing is retried automatically.

- **"No Facebook Page access token configured."**: the credential is empty.
- **`auth_expired`** (Graph code 190): the token is invalid or was revoked; make a new Page token.
- **`permission_denied`** (codes 10, 200–299): the token lacks `pages_manage_posts`, the Page role was lost, or the app
  is not allowed to post (in Development mode only people with a role on the app can be posted for).
- **`rate_limited`** (codes 4, 17, 32, 341, 613): too many calls; wait.
- **`blocked`** (code 368): Facebook blocked the post as spam or policy; nothing was posted.
- **`unsupported`**: this plugin refused the request before calling Facebook (see the list under *Post*).
- The card shows **unconfigured** when the token or Page ID is missing, and **error** when the Page can't be read or the
  token belongs to a different Page.
