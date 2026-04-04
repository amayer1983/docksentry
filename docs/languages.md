# Multi-Language Support

Docksentry includes 16 languages out of the box:

| Flag | Language | Code |
|------|----------|------|
| EN | English | `en` |
| DE | Deutsch | `de` |
| FR | Francais | `fr` |
| ES | Espanol | `es` |
| IT | Italiano | `it` |
| NL | Nederlands | `nl` |
| PT | Portugues | `pt` |
| PL | Polski | `pl` |
| TR | Turkce | `tr` |
| RU | Russkij | `ru` |
| UA | Ukrainska | `uk` |
| SA | al-arabiya | `ar` |
| IN | Hindi | `hi` |
| JP | Nihongo | `ja` |
| KR | Hangugeo | `ko` |
| CN | Zhongwen | `zh` |

## Switching Language

- **Telegram:** `/lang de`, `/lang fr`, etc.
- **Web UI:** Settings page, select from dropdown
- **Environment variable:** `LANGUAGE=de`

Changes via Telegram and Web UI persist across restarts.

## Adding Your Own Language

1. Create a JSON file in the `lang/` directory (e.g. `sv.json` for Swedish)
2. Use `en.json` as a template — copy all keys and translate the values
3. The bot picks up new files automatically — no code changes needed

You can also mount a custom lang directory:

```yaml
volumes:
  - ./my-languages:/app/lang
```

## Contributing Translations

Found a translation error or want to improve an existing language? Open a pull request for `app/lang/*.json`. All language files must have the same keys — the pre-commit check validates this automatically.
