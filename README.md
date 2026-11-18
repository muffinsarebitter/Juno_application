# Juno Travel Concierge

A trip planner and pocket city guide that anyone can open from a link, with no account. It runs on free services: Groq for the AI, GitHub for the files, Render for hosting.

## Put it online

1. **Groq key:** at console.groq.com, open API Keys, click Create API Key, and copy the key (it starts with `gsk_`).
2. **GitHub:** create a repository, click "uploading an existing file", drag in all the files from this folder, and click Commit changes.
3. **Render:** New, then Web Service, then pick the repository. Fill in:
   - Language: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Instance Type: Free
   - Environment Variable: `GROQ_API_KEY` = your key
4. Click Create Web Service and wait for "Live".
5. **Test it:** open your address with `/api/test` at the end. `"working": true` means Juno can reach the AI. Otherwise it says what is wrong.

Share the main address (ending in `onrender.com`) with anyone.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| `/api/test` says the key is not set | In Render, open the service, then Environment, and add `GROQ_API_KEY` |
| `/api/test` says Groq rejected the key | Make a new key at console.groq.com and replace it in Render's Environment |
| "Used up the free AI allowance" | Wait a minute and try again. The free plan allows a limited amount per minute and per day |
| A model "is not available any more" | Add `JUNO_MODELS` in Render's Environment with current model names from Groq's models page, separated by commas |
| The first visit takes up to a minute | Normal on Render's free plan: the app sleeps when unused |

Render's **Logs** tab shows each AI error in plain words, starting with `juno:`.

## Settings (optional, in Render's Environment)

| Variable | Default | What it does |
| --- | --- | --- |
| `GROQ_API_KEY` | none, required | Your Groq key |
| `JUNO_MODELS` | `openai/gpt-oss-120b,openai/gpt-oss-20b` | AI models, tried in order |
| `JUNO_HOURLY_LIMIT` | `40` | AI requests one visitor can make per hour |
| `JUNO_JSON_TOKENS` | `3500` | Longest trip-plan answer. Keep it under about 6000 on the free plan |
| `JUNO_CONTACT` | `juno-app` | Your email, sent to the free map lookup service |

Hotels and places come from the AI's general knowledge, not live listings. Trips are saved on each visitor's own device.
