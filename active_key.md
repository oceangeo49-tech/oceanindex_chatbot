# 챗봇 켜는 명령어
pkill -f '[o]cean_chatbot_app\.py 8000'
cd /home/jy/oceanindex_chatbot
OCEAN_HOST=0.0.0.0 python3 ocean_chatbot_app.py 8000


http://127.0.0.1:8000/



# 랜덤 질문 생성 및 실패 사례 확인(--generate 5는 생성할 대화 수)
cd /home/jy/oceanindex_chatbot

python3 evaluation/qa_run.py \
  --generate 5 \
  --categories irrelevant,typo,unsupported,context_confusion




# GitHub에 올리기(최초)
cd /home/jy/oceanindex_chatbot

git remote add origin https://github.com/oceangeo49-tech/oceanindex_chatbot.git

git remote -v

git push -u origin main


# GitHub에 커밋 절차(최초 이수)
git add . = 이번에 넣을 변경사항 고르기
git commit -m "메시지" = 그 변경사항을 저장 기록으로 남기기
git push = 그 기록을 GitHub에 올리기

git add .
git commit -m "메시지"
git push
