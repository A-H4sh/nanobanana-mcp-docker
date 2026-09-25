FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt constraints.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt \
        -c ${LAMBDA_TASK_ROOT}/constraints.txt

COPY app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
