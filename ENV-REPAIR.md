# Repairing the WSL environment

The screenshots exposed credentials. Revoke/rotate the OpenAI, xAI/Grok, Together, session secret, and owner password before using this copy. Do not paste replacement secrets into chat or commit `.env`.

From WSL, in the project root:

```bash
chmod +x scripts/repair_env.sh
./scripts/repair_env.sh
nano .env
```

Set the new values for `OPENAI_API_KEY`, `XAI_API_KEY`/`GROK_API_KEY`, and `TOGETHER_API_KEY`. The intended roles are GPT-5.4-mini for OpenAI scoring, Grok-4.6 for the optional judge, and Parakeet for transcription. Use models enabled for the API projects. Billing credit alone does not grant a restricted key permission to request every model. The website's Admin provider check uses GPT-5.4-mini when an older `.env` has an empty OpenAI model and reports model-access errors directly.

Start with:

```bash
python -m compileall -q .
python app.py
```

Open `http://127.0.0.1:5059/`. The admin API-key form is owner-only; it validates the selected provider before saving and workers receive the current xAI/OpenAI keys without logging them.
