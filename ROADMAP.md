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
- [ ] "Update available" in the app, one click to update (today: run the install line again)
- [x] Bug reports to GitHub issues from the menu bar, with the technical details filled in (never name, company or calls)
- [ ] The check for client names and private data wired into `release.sh` (the hook is there: `SALES_COACH_LEAK_CHECK`)
- [x] The guide page (GitHub Pages) in the RM look, with the install line and real screenshots from a made-up call

## 0.3: useful from day one for someone new

- [ ] Your own notes per company or person (one file each, matched by mail domain or name), used in the brief
- [ ] Your own standard questions per call type, shown after sales calls with what is still unanswered
- [ ] The window in English as well as Danish (it follows the language setting)
- [ ] Practice calls with an AI lead, using the Mac's own voice
- [ ] A count of how many Macs use it (one anonymous ping a day, no id), like Sunscreen

## 0.4: one codebase for RM too

- [ ] RM's own coach runs on this engine, with RM's data (lead files, talepapir, Google Ads numbers, call
      readiness, MeetGeek comparison) as a private add-on, so improvements made for RM land here
