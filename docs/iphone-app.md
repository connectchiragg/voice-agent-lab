# VALR on your iPhone: the Home Screen app and lock-screen notifications

VALR can live on your iPhone's Home Screen like an app. Once it's there, it can send real notifications to your lock screen, so you can turn off the Telegram alerts if you like.

Every job email and update that shows in VALR's bell also comes to your phone. The ones that need you (an offer, an interview invite, a recruiter asking for something, a take-home) say **Action needed**. Notifications never show pay figures, phone numbers or email addresses.

## What you need

1. An iPhone on **iOS 16.4 or later**: Settings › General › About › iOS Version. Older versions can't get notifications from a Home Screen app.
2. **Safari.** Only Safari can add a web app to the Home Screen with notifications.

## Set it up (once, about 2 minutes)

1. Open **Safari** and go to `https://valr.haciensus.com/valr`. Sign in with Google if asked.
2. Tap the **Share** button: the square with an arrow, at the bottom of the screen.
3. Scroll down and tap **Add to Home Screen**. Check the name says **VALR** and tap **Add**.
4. Go to your Home Screen and open **VALR** from its new blue "V" icon. It opens full screen, without Safari's address bar.
5. Sign in with Google again if it asks. The Home Screen app keeps its own sign-in, separate from Safari.
6. Tap **Settings** (bottom bar) and scroll to **Phone notifications**.
7. Tap **Enable notifications**. iPhone asks *"VALR" Would Like to Send You Notifications*: tap **Allow**.
8. The card now says **On for this iPhone**. Tap **Send test**. Within a few seconds you should get **VALR test notification ✅**, also on the lock screen.

## Choose where alerts go

In the same card, under **Where alerts go**:

1. **Telegram + phone push** (the default): both, as now.
2. **Phone push only**: Telegram alerts and the 09:00 digest stop. Use this once the test notification works. Until at least one phone is subscribed, VALR keeps using Telegram, so nothing is lost.
3. **Telegram only**: no phone notifications.

**Quiet hours** (23:00–08:00, in the Telegram alerts card) hold phone notifications too, except an interview in the next 12 hours. Held ones arrive at 08:00.

## Good to know

1. **Tapping a notification** opens VALR on that lead or page.
2. **Repeated updates from the same email thread replace each other** instead of piling up.
3. **The app icon shows a badge** with the number of unread items.
4. **To turn notifications off** for this iPhone, tap **Turn off** in the card. You can also turn them off in iPhone Settings › Notifications › VALR.
5. **If notifications stop:**
   1. Check iPhone Settings › Notifications › VALR › **Allow Notifications** is on.
   2. Check a Focus mode (Sleep, Work) isn't hiding them.
   3. Open VALR from the Home Screen, go to Settings › Phone notifications and tap **Turn off**, then **Enable notifications** again.
6. **"Add VALR to your Home Screen first"** in the card means you opened Settings in Safari. Open VALR from the Home Screen icon instead.
7. **If you delete the app** from the Home Screen, its notifications stop. VALR notices and forgets that iPhone the next time it sends.
8. **Offline:** without a connection the app shows a simple "You're offline" page. Nothing private is stored on the phone.

## Call recordings from the Home Screen

A separate Home Screen button, **Send recordings to VALR**, sends all your saved Truecaller recordings in one tap. The steps are in VALR at **Settings › Phone call recordings › iPhone shortcut steps** and in `docs/phone-recordings.md`.

## For the server (owner/admin)

1. **Generate the keys once,** on any machine with the repo: `python -m app.webpush keys`. It prints three lines.
2. **Add them to `.env` on the VM:** `VALR_VAPID_PUBLIC`, `VALR_VAPID_PRIVATE` and `VALR_VAPID_SUBJECT` (`mailto:` and your owner address). Both the app and the research worker read `.env`; the worker sends most notifications.
   - Keep the private key secret. Never commit it.
   - New keys mean every phone must tap **Enable notifications** again.
3. **Optional:** `VALR_NOTIFY_DELIVERY` (`both`, `push` or `telegram`) sets the starting choice. Settings overrides it.
4. **Cloudflare Access:** while Access still guards the site, add a **Bypass** policy (like the one for `/hooks/gmail`) for `/manifest.webmanifest`, `/sw.js` and `/static/icons/*`. iPhone fetches these without your login cookie when you add the app. They hold nothing private.
5. **What's sent:** standard Web Push with VAPID. Each notification has a 12-hour TTL and `Urgency: high` for the Action needed ones, `normal` for the rest. Apple's push service delivers it. A subscription that Apple reports gone (404 or 410) is deleted.
