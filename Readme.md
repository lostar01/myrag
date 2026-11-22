## 启动 celery
```
celery -A celery_app worker --loglevel=INFO --pool=solo
```

## 启动 main-api

```
uvicorn main_api:app --reload
```