# retrorender

Ett eget bibliotek för retro- och chiptunemusik. Filerna spelas i originalformat och renderas
i realtid: loopar, speltid och fade styrs av en looppolicy. Musiken spelas i webbläsaren, på
Chromecast/DLNA-apparater i nätet (TV, nätverkshögtalare) eller via Subsonic-appar.

## Innehåll

- [Format och motorer](#format-och-motorer)
- [Installera på TrueNAS SCALE](#installera-på-truenas-scale)
- [Uppdateringar](#uppdateringar)
- [Webbgränssnittet](#webbgränssnittet)
- [Kö och spellistor](#kö-och-spellistor)
- [Spela på TV och högtalare](#spela-på-tv-och-högtalare)
- [Biblioteket](#biblioteket)
- [Metadata: album.toml och titelkartor](#metadata-albumtoml-och-titelkartor)
- [Looppolicy](#looppolicy)
- [joshw.info](#joshwinfo)
- [Subsonic och Music Assistant](#subsonic-och-music-assistant)
- [HTTP-API](#http-api)
- [Utveckling](#utveckling)
- [Kända begränsningar](#kända-begränsningar)

## Format och motorer

| Motor | Format |
| --- | --- |
| libgme | NSF/NSFE, GBS, SPC, HES, KSS, AY, SAP, GYM; VGM/VGZ med bara Sega-chip |
| libvgm (vgm2wav) | VGM/VGZ med övriga chip (YM2151, OPN/OPL, OKI, NES, Game Boy …), S98, DRO |
| libopenmpt | MOD, XM, IT, S3M, MPTM och ett 30-tal till |
| vgmstream | ADX, HCA, BRSTM/BCSTM, AFC, AST, DSP, WEM, TXTP och hundratals till |
| sidplayfp | SID (längder från HVSC:s Songlengths.md5, namn från STIL.txt) |
| jaudio (egen) | Sekvenserad GameCube/Wii-musik: JAudio2 `.bms` + `.baa` (Super Mario Galaxy, Twilight Princess) och JAudio 1 `JaiSeqs.arc` + `JaiInit.aaf` (The Wind Waker) |

**VGM/VGZ** går till libgme om filen bara använder Segas chip (SN76489, YM2413, YM2612),
annars till libvgm. `vgm_engine` i config.toml kan tvinga fram en av dem. Saknas vgm2wav ger
sådana filer ett tydligt fel, och `/health` visar början av `vgm2wav --help` så att
`vgm2wav_args` kan justeras utan ombyggnad.

**jaudio** är skriven utifrån de dekompilerade spelen; formatnoteringar finns i
`docs/jaudio.md`. Instrumentbanken (`.baa` eller `JaiInit.aaf`) hittas i samma mapp,
föräldramappen eller en syskonmapp. Loopar läses exakt ur sekvensen. Ekot är spelets eget
(Galaxy: AudEffector, Wind Waker: FX-scen 0 i `JaiInit.aaf`); mängden sätts av spelet per
bana, så här styrs den av `jaudio_fx_send` (0 = torrt). Sekvenser som bara hör till en
orkesterinspelning (Galaxys `*_midi_cnv` med taktmarkörer) och sekvenser utan toner döljs.
Första genomsökningen av ett stort arkiv (Wind Waker: 93 sekvenser) tar ungefär en minut.

## Installera på TrueNAS SCALE

Imagen byggs av GitHub Actions vid varje ändring på `main`: `ghcr.io/jontep/retrorender:latest`.

1. Skapa ett dataset för metadata, t.ex. `/mnt/tank/apps/retrorender/meta`, och lägg din
   `config.toml` bredvid (utgå från `config.toml` i repot). Båda ska ägas av `apps` (568).
2. Apps → Discover Apps → Custom App → Install via YAML. Utgå från `compose.yaml` och ändra
   sökvägarna.
3. Kontrollera `http://<nas-ip>:8090/health`: laddade motorer, index och metamapp.

Om `compose.yaml`:
- Biblioteket monteras **skrivskyddat** på `/library`. `/library/_synced` (joshw-nedladdningar)
  och `/meta` är skrivbara.
- `network_mode: host` behövs för att Chromecast/DLNA-apparater ska hittas automatiskt.
  Tjänsten nås då på alla NAS:ens gränssnitt, även Tailscale, så använd ett riktigt lösenord.
  Utan host-nätverk: `ports: ["8090:8090"]` och lista apparaterna under `[outputs]`.
- `/meta` är det enda stället med manuellt arbete (album.toml, titles.toml): ta med det i
  snapshots/backup.

## Uppdateringar

Vid varje merge till `main` bygger GitHub Actions en ny image (efter att testerna har gått
igenom). Med `pull_policy: always` hämtas den när appen startas om.

Versionen syns i webbgränssnittet (uppe till höger, och Overview → Build) och i `/health`:
versionsnummer plus commit. Commit ska stämma med senaste commit på `main` (sju första
tecknen). Visar den ett äldre värde är bygget inte klart än; vänta och starta om igen.

När GitHub har byggt en nyare image än den som körs visas **Update ready** uppe till höger i
webbgränssnittet (kollas var femte minut, `[updates]` i config.toml). Starta då om appen.

## Webbgränssnittet

`http://<nas-ip>:8090/`. Inloggning med `user`/`password` från `[web]`, eller från
`[subsonic]` om `[web]` saknas.

- **Overview:** version, motorer, index- och katalogstatus, "Re-index now" och
  "Regenerate all metadata…" (med förhandsvisning och bekräftelse).
- **Library:** bläddra per konsol, år/årtionde, ljudchip och kompositör, sortera efter
  titel, år eller senast tillagt, och sök i album, kompositörer, konsoler och spår.
  "Play all" och "Shuffle" spelar allt som visas; för en kompositör bara deras egna spår.
- **Album:** spårlista med uppspelning, eget omslag ("Change cover…"), redigering av
  `album.toml` (valideras innan den sparas), "Regenerate…", titelkarta och "Export !tags.m3u".
- **Playlists:** se [Kö och spellistor](#kö-och-spellistor).
- **joshw:** se [joshw.info](#joshwinfo).
- **Catalog:** dolda dubbletter, album med samma spelnyckel och spelnycklar per album.

Spelaren längst ner spelar i webbläsaren eller på en apparat i nätet. Play på en låt köar
resten av albumet. Mellanslag pausar och spelar.

**Omslag:** en uppladdad bild (JPEG/PNG) sparas i `/meta` bredvid album.toml
(`cover.jpg`, eller `<fil>.cover.jpg` för album i en enda fil) och går före rippens egen.
"Remove" tar bort den uppladdade bilden.

## Kö och spellistor

- **Kön** (☰ i spelaren) visar vad som spelas och vad som kommer. Dra för att ändra ordning,
  ✕ tar bort, klick hoppar dit. "Clear" behåller bara låten som spelas. Kön sparas i
  webbläsaren och finns kvar efter omladdning.
- **⋯ / ▶ på omslag:** "Play next", "Add to queue", "Add to playlist…" finns på varje låt,
  album, sökresultat och filtrerad vy. ▶ på ett omslag spelar albumet direkt.
- **🔀** blandar resten av kön (låten som spelas står kvar). **🔁** växlar mellan av, hela
  kön och 🔂 en låt.
- Spelas kön på en TV eller högtalare skickas ändringar dit utan att låten som spelas börjar
  om. Byter du från webbläsaren till en apparat medan något spelar, flyttas musiken dit.
- **Spellistor** sparas som en JSON-fil var i `/meta/_playlists/`. Varje rad har låtens id
  och dess sökväg, så en flyttad fil hittas igen; filer som försvunnit visas som "missing"
  men ligger kvar i listan. Samma spellistor syns i Subsonic-appar.

## Spela på TV och högtalare

Välj apparat i listan nere till höger ("This browser" = webbläsaren, "Search for devices…"
söker igen). Retrorender pratar direkt med Chromecast (Google Cast) och DLNA/UPnP-apparater,
utan Home Assistant eller Music Assistant. Apparaten hämtar själv ljudet från `/render`
(FLAC om den klarar det, annars WAV eller MP3) och får titel, album och omslag. Kön går
vidare när en låt är slut; föregående/nästa, paus, stopp och volym styr apparaten.

- Apparater söks automatiskt var tionde minut (kräver host-nätverk). Annars, eller för
  apparater som inte svarar: `cast = [...]` och `dlna = [...]` under `[outputs]`.
- Vissa TV-apparater (t.ex. LG) frågar om lov första gången. Statusraden visar då
  "waiting for the device"; kön väntar i upp till tre minuter.
- `/render` och omslag via `/art/<album>` kräver ingen inloggning, eftersom apparaterna inte
  kan logga in.

## Biblioteket

- **Konsol (genre)** = mappen på översta nivån, med snygga namn från `[platforms]`.
  Ett album kan avvika med `platform = "..."` i sin album.toml.
- **Album** = en mapp med låtar, eller en fil med flera delspår (NSF, SID, `JaiSeqs.arc`).
- **Kompositör (artist)** = från album.toml eller filernas taggar, delat vid `,` `&` `/` `och`.
  Saknas uppgift blir det "Unknown composer".
- Mappar på översta nivån som börjar med `_` eller `.` hoppas över; `_synced` används för
  joshw-nedladdningar. Undermappar i en nedladdning (t.ex. `Seqs`) får albumnamnet från
  arkivet, t.ex. "Super Mario Galaxy (sequenced)".
- **Katalog** (`/meta/catalog.sqlite`): probe-resultat sparas per fil och återanvänds så länge
  storlek och ändringstid är oförändrade.
- **Identiska filer** (SHA-1) visas bara en gång; den första i bokstavsordning (lokala före
  nedladdade) behålls. För TXTP räknas filnamnet och den refererade filen in.
- **Spelnyckel** = konsol + normaliserad titel + region + ljudchip (NSF-expansioner, VGM-chip).
  Versioner samexisterar: "Castlevania III (JP, VRC6)" och "Castlevania III (US)".
- **TXTP:** finns `.txtp`-filer i en mapp är det de som blir spår, och filerna de pekar på
  döljs. Flera TXTP mot samma fil får ett tillägg, t.ex. "Beach Bowl Galaxy (channels 1,2)".

## Metadata: album.toml och titelkartor

Varje album har en `album.toml` i metamappen, som speglar biblioteket:

```
/meta/Wii/Super Mario Galaxy (USA)/album.toml      album = hel mapp
/meta/C64/Commando.sid.album.toml                  album = fil med delspår
```

Den skapas automatiskt. Källor i ordning: egen `titles.toml` → ripens `!tags.m3u` →
inbyggd titelkarta → filens taggar (GD3, SID, NSFE, vgmstream …) → övriga sidofiler
(`.m3u` med `#EXTINF`, NEZplug-m3u med titel/längd/fade per delspår, `.source.json`) →
filnamnet (`05 Vampire Killer.vgz` → spår 5, "Vampire Killer"). Finns en NEZplug-spellista
sätts `only_listed = true`, så att ljudeffekter döljs. Från HVSC används STIL.txt (namn och
kompositör per delspår).

- Första raden har versionsnummer och kontrollsumma. **Orörda** filer genereras om
  automatiskt när genereringen förbättras eller `titles.toml` ändras. Filer du har redigerat
  rörs aldrig.
- "Regenerate…" i webbgränssnittet (eller `/meta/regenerate?path=…`) genererar om ett album;
  en redigerad fil sparas först i `/meta/.backup/<tidpunkt>/`. "Regenerate all metadata…"
  (`/meta/regenerate-all`) visar först vad som påverkas och kräver bekräftelse
  (`&confirm=yes`). Backuper rensas efter `meta_backup_days`.
- Fält per spår (`[[track]]`): `file`, `sub`, `title`, `track`, `artist`, `skip`, `length_s`,
  `intro_s` + `loop_s`, `loops`, `min_s`, `max_s`, `fade_s`. För albumet (`[album]`): `title`,
  `artist`, `year`, `cover`, `platform`, `only_listed` och samma policyfält.

**Titelkartor** översätter interna filnamn (`SMG_galaxy14_strm`) till titlar.
Inbyggda ligger i `app/titlemaps/` (Super Mario Galaxy från Luma's Workshop) och matchas på
albumtitel och konsol. Egna skrivs i fältet "Title map" på albumsidan (sparas som
`titles.toml` i albumets meta-mapp, skrivs aldrig över) och går före den inbyggda kartan.

**Ge tillbaka:** "Export !tags.m3u" laddar ner albumets titlar i vgmstreams format, t.ex. för
att skicka till joshw-tråden på hcs64.com-forumet.

## Looppolicy

Lagren slås ihop i ordningen `[defaults]` → `[engine.<motor>]` → `[ext.<ändelse>]` →
album.toml (album, spår) → URL-parametrar; sist vinner.

- Känd loop: intro + `loops` × loop, förlängd till `min_s`, kapad vid `max_s`, sedan fade.
- Fast längd: spelas klart, kapas bara vid `max_s`.
- Okänd längd: `default_s` med fade.

`/info` visar för varje spår hur längden räknades fram (`reason`).

## joshw.info

Vyn **joshw** listar spelen för konsolerna i `[joshw] systems` (`wii`, `gcn`, `nsf` …), med
titel, år, utvecklare och utgivare ur arkivnamnen. Inget laddas ned förrän du väljer:

- **Listen:** hämtas som *tillfälligt*. Tillfälliga spel över `temp_limit_gb` rensas
  automatiskt, minst nyligen spelade först.
- **Add / Keep:** behålls permanent. En favorit via Subsonic (`star`) gör samma sak.
- **Remove:** tar bort filerna; spelet finns kvar i listan.

Filerna hamnar i `/library/_synced/joshw/<system>/<arkivnamn>/` med en `.source.json`.
Spel som redan finns lokalt markeras "already have it". Hänsyn mot joshw.info: en
nedladdning i taget, `delay_s` sekunders paus, listorna läses om högst var `refresh_hours`.
Kräver `[joshw] enabled = true` och skrivbar `/library/_synced` (ägd av 568:568).

## Subsonic och Music Assistant

Under `/rest/` finns ett OpenSubsonic-API (JSON) som Subsonic-appar och Music Assistants
OpenSubsonic-källa kan använda: artist = kompositör, genre = konsol, album och låtar som
ovan, strömmat som WAV med exakt längd, och samma spellistor som i webbgränssnittet
(läsa, skapa, ändra, ta bort). Användare och lösenord under `[subsonic]`.
Biblioteket indexeras vid start och var `rescan_minutes`.

## HTTP-API

| Väg | Gör |
| --- | --- |
| `/health` | Status, motorer, index, metamapp |
| `/browse?path=Sega` | Mappar och spelbara filer |
| `/info?path=Sega/spel.vgz` | Delspår, metadata, looppunkter, planerad speltid med motivering |
| `/render?path=…&sub=0` | Ljud: WAV med exakt längd, eller `&fmt=flac` / `&fmt=mp3` |
| `/art/<album id>` | Albumomslag |
| `/m3u?path=Sega` | Spellista med render-URL:er (VLC m.fl.) |
| `/catalog` | Katalogstatus, dubbletter, spelnycklar |
| `/meta/show?path=…` | Visa album.toml |

Policyn kan åsidosättas per anrop: `&loops=3&min_s=60&max_s=300&default_s=120&fade_s=5`.
`/api/*` (webbgränssnittets API), `/rescan` och `/meta/regenerate*` kräver inloggning.

## Utveckling

Ändringar görs i grenar och kommer in via pull requests; `main` är skyddad. GitHub Actions
kör testerna på varje PR och vid push till `main`, och bygger imagen när de går igenom.

```
python3 tests/test_txtp_titles.py
python3 tests/test_jaudio.py
python3 tests/test_outputs.py
python3 tests/test_updates.py
python3 tests/test_library.py
```

`/api/engines` och `/api/song/<id>/engine` beskriver vad varje motor kan visa och styra:
`insight` (sequence, pattern, voices, registers eller audio), röst-/kanalnamn och reglage.
Det är grunden för kommande visualisering och motorreglage.

Lokalt utan Docker (kräver libgme/libopenmpt för de motorerna):

```
python3 tests/make_samples.py /tmp/testlib
cd app && RETRO_LIBRARY=/tmp/testlib RETRO_META=/tmp/testmeta RETRO_CONFIG=../config.toml python3 server.py
```

Kod, kommentarer och genererade filer skrivs på engelska.

## Kända begränsningar

- Ingen spolning inom ett spår: Range-förfrågningar besvaras med hela filen.
- SID utan Songlengths.md5 spelas `default_s`.
- jaudio: vibrato/tremolo och IIR/FIR-filter är inte implementerade (används inte av Galaxy).
- Synkad uppspelning i flera rum samtidigt finns inte.
- Subsonic-API:t svarar bara JSON, inte XML.
