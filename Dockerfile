# RCLL AI-Assisted Search — FastAPI on AWS Lambda via the Lambda Web Adapter.
#
# The Web Adapter is a Lambda extension that translates the Function URL event
# into a normal HTTP request to uvicorn, so the app is plain FastAPI with no
# Lambda-specific handler code.
FROM public.ecr.aws/docker/library/python:3.12-slim

# Lambda Web Adapter. VERIFY the tag against the LWA releases before deploy —
# a wrong/missing adapter fails by silently not adapting, not by erroring.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV AWS_LWA_PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/healthz \
    AWS_LWA_ASYNC_INIT=true \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# v1 is buffered (single JSON response). For streamed progress events later,
# add:  AWS_LWA_INVOKE_MODE=response_stream  (and set the Function URL to
# RESPONSE_STREAM in template.yaml).

WORKDIR /var/task

# Dependencies first so the layer caches across frequent data/ + api/ edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# core/, data/, prompts/ must keep the same relative layout: core/catalog.py
# resolves data/ and prompts/ via Path(__file__).parent.parent. app.py is NOT
# copied — it is local-testing-only.
COPY core/ ./core/
COPY data/ ./data/
COPY prompts/ ./prompts/
COPY api/ ./api/

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--no-access-log", "--timeout-keep-alive", "75"]
