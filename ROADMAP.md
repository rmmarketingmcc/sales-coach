# Roadmap

The plan for turning a coach built for one person into one anyone can install. One codebase: the shared coach
here, and each user's own data through add-ons, so every improvement reaches everyone.

## 0.1: the coach runs without anyone's private setup (done)

- [x] Engine split into modules, with no private paths, names or systems in it
- [x] Settings: your name, company, what you sell, language (Danish or English), AI on/off
- [x] AI on your own Claude: Claude Code (subscription) or your own API key (Keychain), or off
- [x] Calendar from a plain `calendar.json` (written by the app, or by an add-on)
- [x] Add-on hooks: context, call types, AI rules, playbook, calendar, booking, day list, after-call, tick
- [x] Booking the agreed next meeting opens it in your calendar (`.ics`); nothing is ever sent by itself
- [x] Window shows your initials and your local time
- [x] A made-up test call (`tests/make_test_call.py`) that runs the whole coach end to end

## 0.2: installable on another Mac

- [x] The recorder, renamed and rebranded (bundle ids, folder), in `recorder/`
- [x] The Sales Coach app in `app/`: reads the Mac Calendar (EventKit) into `calendar.json`; the installer has macOS
      ask once for Calendar, Microphone and the call audio, with a test sound to check the recording works
- [x] One-line installer like Sunscreen's: prebuilt apps, the coach's own Python (uv) in its own folder, the speech
      model downloaded with progress, background services set up, `--uninstall`
- [x] First questions (name, company, what you sell, language, Claude Code found or an API key) in the installer
- [ ] The same as a first-run screen in the window, and a settings screen to change them later
- [x] After the call: audio compressed (AAC, about 15 MB an hour), deleted after 30 days (`keep_audio_days`)
- [ ] After the call: a better full transcript when the Mac is idle; calls with nobody on the other side deleted
- [x] Release flow like Sunscreen's: `./release.sh`, CHANGELOG from the commit subjects, GitHub release
- [x] "Ny version: opdater" in the menu bar (checked once a day), one click; only the coach is swapped while the
      apps' own code is the same, so macOS keeps their permissions (`build.sh` APPS_ID, `install.sh --update`)
- [x] Bug reports to GitHub issues from the menu bar, with the technical details filled in (never name, company or calls)
- [x] The check for client names and private data required by `release.sh` (`SALES_COACH_LEAK_CHECK`)
- [x] The guide page (GitHub Pages) in the RM look, with the install line and real screenshots from a made-up call
- [x] GDPR: a reminder on every call to tell them you record (message to paste in the chat, or "said it"), saved
      with the call; "Stop og slet" for the call going on; delete any saved call; audio and notes deleted after a set time
- [x] The recording notice written in the Google Meet chat by the app itself (Chrome, Brave, Edge, Safari: the browser's
      "Allow JavaScript from Apple Events" once), with the copy button for Zoom, Teams and the rest
- [x] Your own agenda: pasted in the window (during the call, or saved for the next meeting) or read from a list in
      the invite; ticked off by the coach from what is said, Danish or English, or by you; without AI a plain checklist
- [x] The pause after your questions (talk meter frames), with a nudge when you answer them yourself; a nudge for talking too little
- [x] 13 meeting types for more than sales (negotiation, job interview, one-on-one, advice session, support, presentation) plus your own
- [x] "Dine noter" during and after a call, and "Kopiér alt til en AI-chat" (agenda, notes, result, transcript)
- [x] Tell the coach what you want to get better at (presets or your own words), used in cues, the result and Bliv bedre
- [x] The page: an interactive demo, the GDPR rules, open source (MIT) you improve yourself
- [x] One language for users: the page, installer, window, menus and permission texts in Danish
- [x] The page follows the app: version and "What's new" from each release (`docs/changelog.json`), pictures retaken
      from the made-up call with `tests/page_shots.py`

## 0.3: useful from day one for someone new

- [ ] Your own notes per company or person (one file each, matched by mail domain or name), used in the brief
- [ ] Your own standard questions per call type, shown after sales calls with what is still unanswered
- [ ] English for everything a user sees (window, menus, installer, page), following the language setting
- [ ] Practice calls with an AI lead, using the Mac's own voice
- [ ] A count of how many Macs use it (one anonymous ping a day, no id), like Sunscreen

- [ ] Ready add-ons for your own systems: mail (Gmail, Outlook), text messages, a customer list or CRM, so the coach knows
      who you meet before the call, suggests what to ask from what you already know, and updates your notes after it

## 0.4: one codebase for RM too

- [ ] RM's own coach runs on this engine, with RM's data (lead files, talepapir, Google Ads numbers, call
      readiness, MeetGeek comparison) as a private add-on, so improvements made for RM land here
