# Themed programming: requirements (from the user, 2026-10-19)

- **Mozart Monday**: suggest something by Mozart every Monday.
- **Wagner Wednesday**: suggest something by Wagner every Wednesday.
- **Bach Sunday**: Bach every Sunday, especially church music (cantatas, passions, masses).
- **Messiah Monday**: Handel's Messiah every Monday in December. Overrides Mozart Monday in December.
- **Calendar (decided by the user, 2026-10-19)**:
  - Jan: **Mozart month**, after Messiah Mondays in December.
  - Feb: **Handel month**.
  - Mar: **Bach month**.
  - Apr: nothing.
  - May: **Tchaikovsky month**. No Wagner month, since Wagner Wednesdays cover him.
  - Jun: nothing. There aren't enough recordings for a Strauss or Stravinsky month.
  - Jul: **Mahler Mondays**, every Monday in July. The catalog is too small for a full month.
  - Aug: nothing.
  - Sep: **Shostakovich Saturdays and Sundays**.
  - Oct: **Verdi month**.
  - Nov: nothing.
  - Dec: **Beethoven month** (birthday Dec 16), plus Messiah Mondays.
- **Beethoven Advent calendar (December)**: one Beethoven piece per day, opened like an advent-calendar door, themed as a *European* Christmas (Christkindlmarkt, Advent wreath, St Nicholas on Dec 6, Vienna and Bonn). The user said "30 days". A traditional Advent calendar runs Dec 1–24, so the plan should propose a length (24, or 31 for the whole month) and let the user choose. Dec 16, Beethoven's birthday, should be the centerpiece door.
- A composer day may be dropped in a month that belongs to a different composer.
- **Live events**: follow real performances, e.g. play the Ring cycle while Bayreuth is performing it.

## Birth months of the top composers (from researched recordings, 1,137 of 3,201)

| Month | Composers |
|---|---|
| Jan | Mozart (27), Schubert (31) |
| Feb | Handel (23), Rossini (29) |
| Mar | Bach (21 O.S. / 31 N.S.), Ravel (7) |
| Apr | none |
| May | Wagner (22), Tchaikovsky (7), Brahms (7) |
| Jun | R. Strauss (11), Stravinsky (17) |
| Jul | Mahler (7) |
| Aug | none |
| Sep | Shostakovich (25), Dvořák (8) |
| Oct | Verdi (10), J. Strauss II (25) |
| Nov | Donizetti (29), Bellini (3) |
| Dec | Beethoven (16), Puccini (22) |

## Code to read once downloaded from iCloud (currently dataless)
These are guesses from filenames only; none have been read:
- docs/RADIO.md, docs/CALENDAR.md
- server/sethster_server/radio.py, radio_live.py, calendar_api.py, perfdates.py, spotlight.py
- pipeline/context/memoriam.py

## User answers to the plan's open questions (2026-10-19)
1. **Advent length**: traditional, 24 doors (Dec 1–24).
2. **Clashes**: allow multiple special themes on one day. Don't make birthday and Messiah exclusive; do more in a day.
3. **Picks**: the 1940 Krauss Missa is OK. Shuffle picks so they differ each year. Don't pin pieces to dates now.
4. **Catalog**: don't do a special import or Messiah research push. Wait for normal imports, and choose picks when the month or day arrives (pick at runtime from what's in the DB then).
5. **Mozart's Messiah arrangement**: counts. When it plays on a Monday it's a "Mozart Messiah Monday".
6. **Spooky October (2026-10-19)**: in October, add spooky recordings: operas and works with ghosts, devils, demons, witches, beasts, the supernatural (e.g. Der Freischütz, Faust/Mefistofele, Der fliegende Holländer, Macbeth's witches, Rusalka, Dutchman, Hoffmann, Don Giovanni's statue, Night on Bald Mountain, Danse macabre, Symphonie fantastique). Layer it alongside Verdi month (multiple themes per day are allowed), building toward Halloween (Oct 31).
7. **Name**: the spooky October theme is called **"Optoberfest"**.
8. **Pride Month = "Opera Queen Month" (June; added 2026-10-19 13:30)**: a Maria Callas performance every day of June, with Callas art in rainbow/Pride styling. Layer in other performances opera queens love (diva showpieces and cult recordings, e.g. Sutherland, Tebaldi, Price, Caballé, Gencer, Olivero; mad scenes, Norma, Tosca, Traviata, Lucia, Medea). Fills the empty June slot. Pick at runtime from the DB like the other themes; Callas depth (how many distinct Callas broadcasts exist) must be checked so 30 days don't over-repeat. Name TBD with the user ("Opera Queen Month" is the working title).
9. **Pride Month: LGBTQ+ stories (added 2026-10-19)**: also feature operas with gay/queer characters and leads, e.g. Britten's Death in Venice (Aschenbach) and Billy Budd (widely read as gay subtext); Berg's Lulu (Countess Geschwitz, a lesbian character); Wuorinen's Brokeback Mountain; Spears's Fellow Travelers; Blanchard's Fire Shut Up in My Bones. Queer-coded favorites can count separately, e.g. Strauss's Der Rosenkavalier (trouser role Octavian in a love scene with the Marschallin). Characters must be sourced (a reliable source describes the character as gay/queer), not inferred; tag them `lgbtq_story` with the source, same quarantine rules as the Optoberfest supernatural tag.
