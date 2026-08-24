명령만 모은 요약입니다. 각 단계가 왜 그런지는 DEPLOY.md에 있습니다.

[설치 순서(보드마다 1회)]

# 1. 보드 준비물
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl
sudo usermod -aG dialout debian
#    → 여기서 로그아웃/로그인. 안 하면 서비스가 시리얼 포트를 못 엽니다.

# 2. 클론 (경로 고정 — 유닛 파일이 이 경로를 사용함)
git clone --depth 1 --single-branch -b fc-tpcm-relay \
  https://github.com/leegunwoo123/MAVProxy.git /home/debian/fc-tpcm-relay

# 3. 설치 — root로 하지 말 것 (venv가 root 소유가 되면 서비스가 못 씀)
cd /home/debian/fc-tpcm-relay
bash deploy/packaging/bootstrap.sh

# 4. 서비스 등록 및 시작
sudo bash deploy/packaging/install_service.sh

# 5. 확인
systemctl --no-pager --full status fc-tpcm-relay
ss -ltn | grep 6000
git rev-parse --short HEAD          # 이 보드가 도는 버전

[업데이트 순서]

무엇이 바뀌었든(모듈 코드, tpcm.yaml, 유닛 파일) 이 네 줄이 전부입니다.

1. cd /home/debian/fc-tpcm-relay
2. git pull                                        # 클론을 최신으로
3. bash deploy/packaging/bootstrap.sh              # 그 클론을 venv에 설치
4. sudo bash deploy/packaging/install_service.sh   # 유닛 갱신 + reload + 재시작
