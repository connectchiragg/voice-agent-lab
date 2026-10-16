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

## Send them all at once: the "VALR Inbox" folder and a Home Screen button

iPhone apps can't reach into Truecaller's own storage, so the dependable way is: save each recording to one folder in **Files**, then one tap on a Home Screen button sends everything in that folder to VALR and deletes what VALR received. Sending the same file twice is safe: VALR recognises it and doesn't process it again.

### A. Create the folder (once)

1. Open the **Files** app and tap **Browse** (bottom right).
2. Tap **iCloud Drive**. (**On My iPhone** works too; use the same place in step B and step C.)
3. Tap **⋯** (top right), then **New Folder**.
4. Name it `VALR Inbox` and tap **Done**.

### B. After each call: save the recording from Truecaller

1. Open **Truecaller** and open the call recording.
2. Tap the **Share** button.
3. Tap **Save to Files**.
4. Choose **iCloud Drive › VALR Inbox** and tap **Save**.

Keep Truecaller's file name: it often holds the contact and the date. VALR uses the date and time to place the call, and a name to label the other speaker. VALR never keeps digits from a file name, so a phone number in it is never stored or shown.

### C. Build the "Send recordings to VALR" Shortcut (once, about 5 minutes)

You need your upload token (Settings › Phone call recordings › **Show upload token**), the same one as above.

1. Open **Shortcuts**, tap **+**, tap the name at the top, choose **Rename**, and type `Send recordings to VALR`.
2. **Text** action: search `Text`, add it, and type `yes`. Long-press the action's output name (**Text**), choose **Rename**, and call it `Ask first`. To skip the question later, change `yes` to `no`.
3. **Get Contents of Folder**: search `Get Contents of Folder`, add it, tap **Folder**, and pick **iCloud Drive › VALR Inbox**. Leave *Recursive* off.
4. **Filter Files**: search `Filter Files`, add it (input: *Contents of Folder*). Tap **Add Filter** four times and set them to **File Extension is** `m4a`, `mp3`, `wav` and `aac`. Tap **all** at the top and change it to **any**.
5. **If**: search `If` and add it. Input: **Ask first**. Condition: **is**. Value: `yes`.
6. Inside that If, add **Show Alert**. Message: `Send these recordings to VALR and delete them from this iPhone?` Turn on **Show Cancel Button**. (Cancel stops the whole Shortcut.) Leave the **Otherwise** part empty.
7. **Repeat with Each**: search `Repeat with Each` and add it *below* **End If**. Input: **Files** (the output of Filter Files).
8. Inside the repeat, add **Get Contents of URL** and set it up:
   - **URL:** `https://valr.haciensus.com/hooks/recording-upload`
   - Tap **›** to show more. **Method:** POST.
   - **Headers:** **Add new header**. Key `Authorization`, value `Bearer ` (one space), then paste your upload token.
   - **Request Body:** Form.
   - **Add new field › File**. Key `file`, value **Repeat Item**.
   - **Add new field › Text**. Key `filename`. For the value, tap it, choose **Repeat Item**, then tap **Repeat Item** again and pick **Name**.
9. Still inside the repeat, add **Get Dictionary Value**. Set it to get **Value** for key `result` in **Contents of URL**.
10. Add **If**. Input: **Dictionary Value**. Condition: **is**. Value: `ok`.
11. Inside this If (above **Otherwise**):
    - Add **Delete Files**. Input: **Repeat Item**. Tap **›** and turn **off** *Confirm Before Deleting* (you already confirmed once, in step 6).
    - Add **Add to Variable**. Input: **Repeat Item**. Variable name: `Sent`.
12. Leave **Otherwise** empty: a file that didn't reach VALR stays in the folder for next time.
13. After **End Repeat**, add **Count**. Set it to count **Items** in **Sent**.
14. Add **Show Notification** (or **Show Result**). Text: `Sent`, then the **Count** variable, then `recordings`.
15. Tap **Done**.

The first run asks *Allow "Send recordings to VALR" to access VALR Inbox?* and *…to send files to valr.haciensus.com?* and *…to delete files?* Tap **Always Allow** each time.

Deleted files go to **Files › Browse › Recently Deleted** for 30 days, so nothing is lost by mistake.

### D. Put the button on the Home Screen

1. In **Shortcuts**, long-press **Send recordings to VALR** and tap **Details** (or open it and tap **ⓘ** at the bottom).
2. Tap **Add to Home Screen**.
3. Choose an icon and colour if you like, then tap **Add** (top right).

Now: after calls, save the recordings to **VALR Inbox** (step B). When you're ready, tap the button once.

### Optional, once: does Truecaller's iCloud backup give you a folder?

This depends on your Truecaller version, so check it once:

1. In **Truecaller**, open **Settings** and look for **Call Recording** or **Backup**. If you see an iCloud backup for recordings, turn it on and let it run.
2. Open **Files › Browse › iCloud Drive** and look for a **Truecaller** folder with your recordings as audio files.
3. If it's there, you can point the Shortcut's **Get Contents of Folder** (step C.3) at that folder instead of **VALR Inbox**. Be careful: the Shortcut then deletes from Truecaller's own backup after each upload. If you'd rather keep them, remove the **Delete Files** action.
4. If there's no such folder (most versions keep the backup private to the app), use **Save to Files** (step B). That way always works.

## Good to know

- **Re-sends:** a file VALR already has answers `"duplicate": true` and isn't processed again.
- **Files:** m4a, mp3, wav or aac, up to 100 MB. That's several hours of call; 100 MB is also Cloudflare's limit for one request.
- **Speakers:** the person on the other end is labelled with the name you gave, otherwise *Interviewer*. VALR recognises you when you introduce yourself ("this is <your name>"), when you're greeted by name, or else as the person who speaks most.
- **Privacy:** only you can play the audio, and only while signed in. The audio is encrypted in the database and deleted after 7 days (`VALR_PHONE_AUDIO_DAYS`). The token is stored only as a hash.
- **Errors in Show Result:**
  - **401:** the token is wrong or was rotated. Paste the current one.
  - **413:** the file is too large.
  - **415:** the file isn't one of the audio types above.
  - **429:** too many uploads in an hour (60 per hour by default).

## For the server (owner/admin)

- **Cloudflare Access:** add a **Bypass** policy for the path `/hooks/recording-upload` in the *voice-telnyx* Access application, the same way as `/hooks/gmail`. The Shortcut cannot sign in with Google, so the route checks its own bearer token.
- **Environment, app container:**
  - `VALR_UPLOAD_TOKEN`: the first token. Use a long random value, for example `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. After that, Settings can rotate it without editing the environment.
  - `VALR_TOKEN_KEY`: already set; it encrypts the audio.
  - Optional: `VALR_PHONE_MAX_MB` (100) and `VALR_UPLOAD_PER_HOUR` (60 per IP).
- **Environment, research worker:**
  - `DEEPGRAM_API_KEY`.
  - Optional: `VALR_PHONE_LANGUAGE` (`en-IN`; `multi` for Hindi–English calls), `VALR_PHONE_MODEL` (`nova-3`), `VALR_PHONE_DEEPGRAM_HOST` (`api.deepgram.com`), `VALR_PHONE_AUDIO_DAYS` (7), `VALR_PHONE_LOOP_S` (30), and `VALR_PHONE_RECORDINGS=0` to turn processing off.
