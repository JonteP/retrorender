# retrorender v0.8

Renderar retro- och chiptunefiler i realtid över HTTP. Detta är fas 1 i designdokumentet "Retrobibliotek".

| Motor | Format | Status |
| --- | --- | --- |
| libgme | NSF/NSFE, GBS, SPC, HES, KSS, AY, SAP, GYM; VGM/VGZ med bara Sega-chip | testad |
| libvgm (vgm2wav) | VGM/VGZ med övriga chip: YM2151, OPN/OPL, OKI, NES, Game Boy m.fl.; S98, DRO | otestad (kräver bygget) |
| libopenmpt | MOD, XM, IT, S3M, MPTM och ca 30 till | testad |
| vgmstream | ADX, HCA, BRSTM/BCSTM, AFC, AST, DSP, WEM m.fl. | otestad (kräver bygget) |
| sidplayfp | SID | otestad, kontrollera CLI-flaggorna |

## Installera på TrueNAS SCALE

1. Kopiera mappen till ett dataset, t.ex. `/mnt/tank/apps/retrorender`.
2. Ändra sökvägarna i `compose.yaml` (biblioteket och config.toml).
3. Bygg imagen i TrueNAS-skalet (System -> Shell):
   ```
   cd /mnt/tank/apps/retrorender && sudo docker build -t retrorender:0.5 .
   ```
4. Apps -> Discover Apps -> Custom App -> Install via YAML. Klistra in `compose.yaml`.
5. Kontrollera: `http://<nas-ip>:8090/health` visar vilka motorer som laddades.

Containern läser biblioteket skrivskyddat och kör som användaren `apps` (568).

## joshw.info (nytt i v0.8)

Vyn **joshw** i webbgränssnittet listar spelen för de konsoler som står i `[joshw] systems` i config.toml (`wii`, `gcn`, `nsf`, …). Titel, år, utvecklare och utgivare läses ur arkivnamnen. Inget laddas ned förrän du väljer:

- **Listen:** hämtar och packar upp spelet som *tillfälligt*. Tillfälliga spel över `temp_limit_gb` rensas automatiskt, det minst nyligen spelade först.
- **Add / Keep:** spelet behålls permanent i biblioteket. En favorit (hjärta) i Music Assistant gör samma sak.
- **Remove:** tar bort de nedladdade filerna. Spelet finns kvar i listan och kan hämtas igen.

Filerna hamnar i `/library/_synced/joshw/<system>/<arkivnamn>/`, med en `.source.json` som ger rätt titel och år i album.toml. Konsolnamnen (genre) kommer från en inbyggd lista, som kan ändras under `[joshw.platforms]`. Spel som redan finns i ditt eget bibliotek med samma titel och konsol markeras "already have it". Identiska filer döljs som vanligt, och den lokala kopian vinner.

Hänsyn mot joshw.info, som drivs av frivilliga: en nedladdning i taget, `delay_s` sekunders paus mellan anropen, och listorna läses om högst en gång per `refresh_hours`.

Krav:
- `/library/_synced` monterad **skrivbar**, efter den skrivskyddade `/library` i compose. Mappen ska ägas av 568:568.
- `[joshw] enabled = true` i config.toml.

## Webbgränssnitt (nytt i v0.7)

Öppna `http://<nas-ip>:8090/`. Inloggningen använder `user` och `password` från `[subsonic]`, eller från ett eget avsnitt `[web]` om du vill ha separata uppgifter:

```toml
[web]
user = "admin"
password = "något-annat"
```

- **Overview:** version, motorer, index- och katalogstatus, knappen "Re-index now" och "Regenerate all metadata…" (med förhandsvisning och bekräftelse).
- **Library:** bläddra per konsol och kompositör, och sök i album, kompositörer, konsoler och spår.
- **Album:** spårlista med provlyssning i webbläsaren, redigering av `album.toml` (valideras innan den sparas, så att en felaktig fil aldrig skrivs) och "Regenerate…".
- **Catalog:** dolda dubbletter, album med samma spelnyckel och spelnyckeln för alla album.

Det som ändrar något kräver inloggning: allt under `/api/`, samt `/rescan`, `/meta/regenerate` och `/meta/regenerate-all`. `/health`, `/info`, `/render`, `/browse`, `/m3u` och `/meta/show` är fortfarande öppna, så att Home Assistant och Chromecast kan använda render-URL:er utan inloggning.

## Katalog i SQLite (nytt i v0.6)

`/meta/catalog.sqlite` sparar probe-resultat per fil. Vid omstart läses de från databasen i stället för att varje fil probas igen, så länge storlek och ändringstid är oförändrade. Filer som har försvunnit tas bort ur katalogen vid nästa indexering.

- **Identiska filer:** SHA-1 räknas i bakgrunden. Filer med exakt samma innehåll visas bara en gång. Den första i alfabetisk ordning behålls.
- **Spelnyckel** = konsol + normaliserad titel + region + ljudchip. Regionen läses från taggar som `(Japan)` eller `(USA)` i albumnamn och mappnamn. Ljudchip läses från NSF-headern (VRC6, VRC7, FDS, MMC5, N163, S5B) och från VGM-headern.
- **Versioner samexisterar:** album med samma namn men olika region eller chip får ett tillägg, t.ex. "Castlevania III (JP, VRC6)" och "Castlevania III (US)".
- **`/catalog`** visar katalogstatus, identiska filer, dolda dubbletter och spelnycklar per album.
- Mappar på översta nivån som börjar med `_` eller `.` hoppas över. `_synced` är reserverad för kommande synk.

## Artister och konsoler (nytt i v0.5)

- **Artist** = kompositör. Albumartist hämtas från `artist` i album.toml, annars från låtarnas kompositörer. Saknas uppgift blir det "Okänd kompositör". Flera namn delas vid `,` `&` `/` `och`, så ett album hamnar under varje kompositör.
- **Genre** = konsol. Det är plattformsmappen, med snygga namn från `[platforms]` i config.toml. Ett album kan avvika med `platform = "..."` i sin album.toml.
- I MA: Artister → kompositör → alla album. Genrer → konsol → allt för den konsolen.

Nya metadatakällor:
- **NEZplug-m3u** (`spel.nsf::NSF,3,Titel,2:30,,10`) för NSF/GBS/KSS/HES: titel, längd och fade per delspår. Om en sådan spellista finns sätts `only_listed = true`, så att ljudeffekter som inte är musik döljs. Längden räknas som speltid plus fade, alltså samma tolkning som i NEZplug och libgme.
- **Spellisttaggar** i NEZplug-m3u (`# @COMPOSER`, `# @DATE`, `# @TITLE`) ger kompositör, år och speltitel. `@ARTIST` används bara som reserv, eftersom fältet ofta innehåller utgivaren.
- **STIL.txt** från HVSC: namn (NAME) och kompositör (AUTHOR) per delspår. Den hittas automatiskt bredvid Songlengths.md5.

**Tvingad omgenerering:** `/meta/regenerate-all` (hela biblioteket) eller `/meta/regenerate-all?path=Wii` (en konsol) visar först en varning med hur många filer som påverkas och vilka du har redigerat. Inget händer förrän du öppnar bekräftelselänken (`&confirm=ja`). Alla filer flyttas först till `/meta/.backup/<tidpunkt>/`. Även `/meta/regenerate` på ett enskilt album sparar en kopia om filen var redigerad. Gamla backuper rensas automatiskt efter `meta_backup_days` (90 dagar).

Från v0.5.1 har varje genererad album.toml en kontrollsumma på första raden. Filer som inte har ändrats för hand genereras om automatiskt när genereringen förbättras. Filer du har redigerat rörs aldrig. Filer från v0.4 saknar kontrollsumma: ta bort dem som du inte har ändrat, så skapas nya.

## Metadata: album.toml (nytt i v0.4)

Titlar, spårordning, dolda spår och looppolicy styrs av en `album.toml` per album i metamappen (`/meta`). Strukturen speglar biblioteket:

```
/meta/Wii/Super Mario Galaxy (USA)/album.toml      album = hel mapp
/meta/C64/Commando.sid.album.toml                  album = fil med delspår
```

Filen skapas automatiskt vid första indexeringen. Källorna används i den här ordningen:
1. Filens egna taggar (GD3, SID-header, NSFE, vgmstream ...).
2. Rippens sidofiler: `!tags.m3u` (`# @ALBUM`, `# %TITLE`) och `.m3u` med `#EXTINF`.
3. Filnamnet, städat: `05 Vampire Killer.vgz` blir spår 5, "Vampire Killer".

En befintlig fil skrivs **aldrig över**. Vill du generera om: `/meta/regenerate?path=<mapp>`, eller ta bort filen. Ändringar slår igenom vid nästa indexering, eller direkt via `/rescan`.

Fält per spår (`[[track]]`): `file`, `sub`, `title`, `track`, `artist`, `skip`, `length_s` (fast längd), `intro_s` + `loop_s` (egen loop), `loops`, `min_s`, `max_s`, `fade_s`.
Fält för albumet (`[album]`): `title`, `artist`, `year`, `cover`, `only_listed`, samt samma policyfält som gäller hela albumet.

Policyordning: config -> motor -> filändelse -> album -> spår -> URL-parametrar.

Metamappen är det enda stället med manuellt arbete. Lägg den i ett dataset som täcks av snapshots och backup. Containern (användare 568) måste ha skrivrätt dit. `/health` visar `meta.writable`.

## Music Assistant via OpenSubsonic (nytt i v0.3)

retrorender talar Subsonic-protokollet under `/rest/`. Music Assistants inbyggda källa "OpenSubsonic" kan därför läsa biblioteket utan att MA ändras.

- **Artist** = plattformsmappen på översta nivån (`X68000`, `MegaDrive`, `C64` ...)
- **Album** = en spelmapp, eller en fil med flera delspår (NSF, SID)
- **Låt** = ett delspår, med längd enligt looppolicyn. Strömmas som WAV.

Biblioteket indexeras vid start och sedan var `rescan_minutes`. `/health` visar indexets storlek.

Inställningar i config.toml:
```toml
[subsonic]
enabled = true
user = "musicassistant"
password = "byt-mig"
rescan_minutes = 60
```

I MA: Inställningar -> Musikkällor -> Lägg till -> OpenSubsonic. Ange NAS:ens adress, port 8090 och användare och lösenord enligt ovan.

Andra Subsonic-appar (t.ex. på mobilen) fungerar om de använder JSON-formatet. XML-svar finns inte ännu.

## Motorval för VGM/VGZ (nytt i v0.2)

Tjänsten läser VGM-headern och ser vilka chip filen använder. Filer med bara SN76489, YM2413 och YM2612 (Sega) går till libgme. Allt annat går till libvgm. Inställningen `vgm_engine` i config.toml kan tvinga fram en av motorerna. `/info` visar chippen under `extra.chips` för filer som spelas med libvgm.

Saknas vgm2wav ger X68000-filer och liknande ett tydligt fel i stället för tystnad, och `/browse` visar `"engine": "saknas"`.

`/health` visar början av `vgm2wav --help` under `diagnostics`. Om flaggorna inte stämmer ändrar du `vgm2wav_args` i config.toml och startar om. Ingen ombyggnad behövs.

## API

| Väg | Gör |
| --- | --- |
| `/health` | Status, laddade och saknade motorer |
| `/browse?path=Sega` | Mappar och spelbara filer |
| `/info?path=Sega/spel.vgz` | Delspår, metadata, looppunkter och planerad speltid med motivering |
| `/render?path=...&sub=0` | Ljud. WAV med exakt längd (standard) eller `&fmt=flac` |
| `/m3u?path=Sega` | Spellista med render-URL:er, öppna i VLC |

Policyn kan åsidosättas per anrop: `&loops=3&min_s=60&max_s=300&default_s=120&fade_s=5`.

## Testa på högtalarna från Home Assistant

Utvecklarverktyg -> Åtgärder:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.argon_forte_a5   # din Chromecast-entitet
data:
  media_content_id: "http://192.168.1.10:8090/render?path=Sega/spel.vgz&sub=0"
  media_content_type: music
```

## Looppolicy (config.toml)

Ordningen är `[defaults]` -> `[engine.<motor>]` -> `[ext.<ändelse>]` -> query-parametrar, och det sista lagret vinner.

- Spår med känd loop: intro + `loops` x loop, förlängd till `min_s` och kapad vid `max_s`, sedan fade.
- Spår med fast längd: spelas klart, kapas bara vid `max_s`.
- Okänd längd: `default_s` med fade.

`/info` visar för varje spår hur längden räknades fram (`reason`).

## Kända begränsningar i v0.1

- Ingen sökning inom ett spår: Range-förfrågningar besvaras med hela filen.
- Per-spår-overrides (YAML-fil) kommer i fas 2.
- SID utan Songlengths.md5 spelas `default_s`.

## Lokalt test utan Docker

```
python3 tests/make_samples.py /tmp/testlib
cd app && RETRO_LIBRARY=/tmp/testlib RETRO_CONFIG=../config.toml python3 server.py
```
