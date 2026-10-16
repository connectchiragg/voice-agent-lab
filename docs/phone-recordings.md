# Phone call recordings: Truecaller → VALR

When Truecaller records a phone interview, you can send the recording to VALR in two taps. VALR then:

1. stores the audio encrypted,
2. turns it into a transcript with speakers (Deepgram),
3. works out the company,
4. adds the call to **Past interviews** with a player,
5. has the coach review it and sends the post-call report on Telegram, the same as a Fathom recording.

The audio is deleted 7 days after processing. The transcript and the review stay.

```
Truecaller (recording)  →  Share  →  "Send to VALR" Shortcut  →  VALR  →  Past interviews + Telegram report
```

## What you need

- Truecaller Premium on the iPhone, with call recording working.
- **Your upload token from VALR Settings:** open Settings › Phone call recordings and press **Show upload token**. It is shown only once, so copy it straight into the Shortcut (step 6 below). If you lose it, press **Rotate token**: that gives you a new one, and the old one stops working.

## Create the Shortcut (once, about 3 minutes)

1. Open the **Shortcuts** app and tap **+** (top right).
2. Tap the name at the top, choose **Rename**, and call it `Send to VALR`.
3. Tap the **ⓘ** (Details) button at the bottom and turn on **Show in Share Sheet**. Tap **Done**.
4. At the top, the shortcut now says *Receive **Any** input from **Share Sheet***.
   - Tap **Any**, choose **Clear**, then select only **Media** and **Files**. Tap **Done**.
   - Tap the *If there's no input* line and choose **Stop and Respond**.
5. Tap **Search Actions**, type `Get Contents of URL`, and add it.
6. Set it up:
   - **URL:** tap *URL* and type `https://valr.haciensus.com/hooks/recording-upload`.
   - Tap the **›** arrow to show more options.
   - **Method:** POST.
   - **Headers:** tap **Add new header**. Key `Authorization`. Value `Bearer ` (with one space after it), then paste your upload token straight after the space.
   - **Request Body:** Form.
   - Tap **Add new field** and choose **File**. Key `file`. For the value, tap it and pick **Shortcut Input**.
   - *Optional:* tap **Add new field**, choose **Text**, key `name`, and type the other person's name. This is useful for a single recruiter. A better way is in the next step.
7. *Optional, recommended:* add an **Ask for Input** action *above* "Get Contents of URL".
   - Set the prompt to *Who was the call with? (optional)*.
   - Then use a Text field with key `name` and value **Provided Input**.
   - VALR uses the name to match the call to a lead and to label the speakers.
8. Add a **Show Result** action at the end (value: *Contents of URL*). After each upload you'll see `"ok": true` and "Got it".
9. Tap **Done**.

The first time it runs, iOS asks *Allow "Send to VALR" to send 1 file to valr.haciensus.com?* Tap **Always Allow**.

## Send a recording from Truecaller

1. Open **Truecaller** and go to your call recordings (the **Recordings** list, or the call in **Calls** history with the recording icon).
2. Open the recording and tap the **Share** button.
3. Choose **Send to VALR**.
   - If it isn't in the row of apps, scroll the share sheet down and tap **Edit Actions…** to add it.
   - If Truecaller only offers **Save to Files**, save it, then share it from the **Files** app the same way.
4. Wait for "Got it". A few minutes later the call appears under **Interviews › Past**, and the Telegram report follows once the coach has reviewed it.

Tip: send the recording soon after the call. VALR places the call in time from the upload moment and the recording's length, and uses that time to match it to a calendar interview.

## Good to know

- **Files:** m4a, mp3, wav or aac, up to 100 MB. That's several hours of call; 100 MB is also Cloudflare's limit for one request.
- **Speakers:** the person on the other end is labelled with the name you gave, otherwise *Interviewer*. VALR recognises you when you introduce yourself ("this is <your name>"), when you're greeted by name, or else as the person who speaks most.
- **Privacy:** only you can play the audio, and only while signed in. The audio is encrypted in the database and deleted after 7 days (`VALR_PHONE_AUDIO_DAYS`). The token is stored only as a hash.
- **Errors in Show Result:**
  - **401:** the token is wrong or was rotated. Paste the current one.
  - **413:** the file is too large.
  - **415:** the file isn't one of the audio types above.
  - **429:** too many uploads in an hour.

## For the server (owner/admin)

- **Cloudflare Access:** add a **Bypass** policy for the path `/hooks/recording-upload` in the *voice-telnyx* Access application, the same way as `/hooks/gmail`. The Shortcut cannot sign in with Google, so the route checks its own bearer token.
- **Environment, app container:**
  - `VALR_UPLOAD_TOKEN`: the first token. Use a long random value, for example `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. After that, Settings can rotate it without editing the environment.
  - `VALR_TOKEN_KEY`: already set; it encrypts the audio.
  - Optional: `VALR_PHONE_MAX_MB` (100) and `VALR_UPLOAD_PER_HOUR` (20 per IP).
- **Environment, research worker:**
  - `DEEPGRAM_API_KEY`.
  - Optional: `VALR_PHONE_LANGUAGE` (`en-IN`; `multi` for Hindi–English calls), `VALR_PHONE_MODEL` (`nova-3`), `VALR_PHONE_DEEPGRAM_HOST` (`api.deepgram.com`), `VALR_PHONE_AUDIO_DAYS` (7), `VALR_PHONE_LOOP_S` (30), and `VALR_PHONE_RECORDINGS=0` to turn processing off.
