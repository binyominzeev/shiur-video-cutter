# shiur-video-cutter

AI-támogatott vágás tanítás-videókhoz (Kdenlive). Az LLM csak vágási pontokat javasol, a vágást a script végzi a Kdenlive projektfájlon.

## Workflow

1. **SRT** készítése a nyers videóról (automatikus átírás).
2. **LLM**: az SRT-t és az [llm-prompt.md](llm-prompt.md)-t odaadod Claude-nak / ChatGPT-nek → kapsz egy **vágási JSON-t**:
   - `cuts` – biztos vágások,
   - `questions` – kérdéses részek (te döntesz).
3. **Vágatlan `.kdenlive` projekt** kézi összeállítása (a videó a timeline-on, vágás nélkül).
4. **Script futtatása**:
   ```bash
   python kdenlive_vagas.py vagatlan.kdenlive vagas.json -o vagott.kdenlive
   ```
   A `questions` részeknél párbeszédablak jön: visszajátszható a rész, majd **Maradjon** / **Kivágom**.
5. A `vagott.kdenlive` megnyitása Kdenlive-ban, **kézi renderelés**.

## Telepítés

- Python 3
- Opcionális, a kérdező ablakhoz: `pip install PySide6` (nélküle konzolos kérdezés + külső lejátszó)

## Hasznos kapcsolók

| Kapcsoló | Jelentés |
|---|---|
| `--answers ask\|suggest\|keep\|cut` | kérdések kezelése (alap: `ask`) |
| `--decisions FILE` | korábbi döntések újrahasználata |
| `--decisions-out FILE` | döntések mentése (alap: `<kimenet>.decisions.json`) |
| `--margin SEC` | ennyi mp-et meghagy a vágások szélén |
| `--context SEC` | előzmény/utózmány a lejátszóban (alap: 5) |
| `--video FILE` | kérdéseknél lejátszandó videó |
| `--no-audio` | hang nélküli lejátszás |
| `--dry-run` | csak terv, nem ír fájlt |

A kivágott részek "ripple" módon esnek ki (nincs lyuk a timeline-on).
A kimeneti projektből minden timeline-jelölő törlődik.

## Fájlok

- `kdenlive_vagas.py` – a vágó script
- `llm-prompt.md` – az LLM-nek szánt szabálykönyv
- `beresit-sze_*` – példa bemenet/kimenet
- `archiv/` – régi anyagok
