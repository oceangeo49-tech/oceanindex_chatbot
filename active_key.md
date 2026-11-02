# 챗봇 키는 명령어
pkill -f '[o]cean_chatbot_app\.py 8000'
cd /home/jy/oceanindex_chatbot
OCEAN_HOST=0.0.0.0 python3 ocean_chatbot_app.py 8000


http://127.0.0.1:8000/



# 랜덤 질문 생성 및 실패 사례 확인(--generate 5 만들어내는 질문 갯수)
cd /home/jy/oceanindex_chatbot

python3 evaluation/qa_run.py \
  --generate 5 \
  --categories irrelevant,typo,unsupported,context_confusion