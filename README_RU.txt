GOOGLE ADS AI AGENT — ПЕРВАЯ БЕЗОПАСНАЯ ВЕРСИЯ

Что умеет:
1. Проверяет работу сервера.
2. Показывает доступные Google Ads аккаунты.
3. Показывает статистику кампаний.
4. Готовит изменение бюджета или статуса кампании.
5. Выполняет изменение только по одноразовому токену после явного подтверждения.
6. Ничего не удаляет.

Нужные секреты Cloud Run:
AGENT_API_KEY
GOOGLE_ADS_DEVELOPER_TOKEN
GOOGLE_ADS_CLIENT_ID
GOOGLE_ADS_CLIENT_SECRET
GOOGLE_ADS_REFRESH_TOKEN
GOOGLE_ADS_LOGIN_CUSTOMER_ID — только если используется управляющий MCC аккаунт.

Важно:
- Не загружайте client_secret JSON в публичный GitHub.
- Не вставляйте секреты в openapi.yaml.
- После развёртывания замените REPLACE_WITH_CLOUD_RUN_URL в openapi.yaml.
