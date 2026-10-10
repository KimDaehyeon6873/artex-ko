#!/usr/bin/env bash
# 로컬 VM의 소스 빌드 → DB 준비 → ARTEX 기동. 호스트 요구 사항: Docker + Compose v2.20+.
set -euo pipefail
umask 077

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STATE="$ROOT/.local-vm"
ENV_FILE="$STATE/compose.env"
PROJECT=artex-local-vm
COMPOSE_FILE="$ROOT/compose.local-vm.yaml"

usage() {
  cat <<'HELP'
사용법: ./local-vm.sh [up|build|start|stop|down|status|logs]
  up      소스 빌드 후 DB/ARTEX 기동, 준비 완료까지 대기 (기본값)
  build   소스에서 로컬 Docker 이미지만 빌드
  start   기존 이미지로 기동 (재빌드/이미지 다운로드 없음)
  stop    컨테이너 중지, 데이터 보존
  down    컨테이너/네트워크 제거, 데이터 볼륨 보존
  status  컨테이너 상태 확인
  logs    최근 로그 100줄부터 실시간 확인 (Ctrl-C로 종료)

처음 실행할 때 .local-vm/compose.env 에 DB 비밀번호를 자동 생성합니다.
관리 UI/기록 프록시는 VM의 127.0.0.1 에만 공개하며 DB 포트는 공개하지 않습니다.
HELP
}

die() { printf '[local-vm] %s\n' "$*" >&2; exit 1; }
info() { printf '[local-vm] %s\n' "$*"; }

command_name=${1:-up}
[ "$#" -le 1 ] || { usage >&2; exit 2; }
case "$command_name" in
  -h|--help|help) usage; exit 0 ;;
  up|build|start|stop|down|status|logs) ;;
  *) usage >&2; exit 2 ;;
esac

command -v docker >/dev/null 2>&1 || die "Docker Engine 과 Docker Compose 플러그인을 먼저 설치하세요. README의 로컬 VM 절을 참고하세요."
docker compose version >/dev/null 2>&1 || die "Docker Compose 플러그인이 필요합니다 (v2.20 이상)."
docker compose up --help | grep -q -- --wait-timeout || die "Docker Compose v2.20 이상으로 업데이트하세요 (--wait-timeout 필요)."
docker info >/dev/null || die "Docker 데몬에 연결할 수 없습니다. 데몬 실행 상태와 현재 사용자의 Docker 접근 권한을 확인하세요."

# env 파일을 셸 코드로 source 하지 않는다. 호스트 환경 변수가 저장된 DB 비밀번호를
# 덮어쓰지 않도록 이 워크플로의 변수만 제거하고 Compose 에서 파싱하게 한다.
compose() (
  unset ARTEX_VM_DB_PASSWORD ARTEX_VM_HTTP_PORT ARTEX_VM_PROXY_PORT
  docker compose --project-name "$PROJECT" --project-directory "$ROOT" \
    --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
)

mkdir -p "$STATE"
[ ! -L "$STATE" ] || die ".local-vm 은 심볼릭 링크일 수 없습니다."
chmod 700 "$STATE"
locked=0
# 조회/로그 스트리밍은 기동이나 중지를 막지 않는다.
case "$command_name" in
  status|logs) ;;
  *)
    mkdir "$STATE/lock" 2>/dev/null || die "다른 local-vm.sh 가 실행 중입니다. 비정상 종료였다면 프로세스가 없는지 확인한 뒤 .local-vm/lock 디렉터리를 제거하세요."
    locked=1
    ;;
esac
cleanup() {
  if [ "$locked" -eq 1 ]; then rmdir "$STATE/lock" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[ ! -L "$ENV_FILE" ] || die "compose.env 는 심볼릭 링크일 수 없습니다."
if [ ! -e "$ENV_FILE" ]; then
  case "$command_name" in
    up|build) ;;
    *) die "로컬 VM 설정이 없습니다. 먼저 ./local-vm.sh 를 실행하세요." ;;
  esac
  # 비밀번호 파일을 잃었을 때 기존 DB의 비밀번호와 다른 값을 생성하지 않는다.
  existing_volumes=$(docker volume ls --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Name}}')
  [ -z "$existing_volumes" ] || die "기존 $PROJECT 볼륨이 있지만 compose.env 가 없습니다. 백업한 .local-vm/compose.env 를 복원하세요."
  password=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  [ "${#password}" -eq 64 ] || die "DB 비밀번호 생성에 실패했습니다."
  # noclobber: 이미 존재하는 파일은 절대 덮어쓰지 않는다.
  (set -o noclobber; printf 'ARTEX_VM_DB_PASSWORD=%s\nARTEX_VM_HTTP_PORT=8787\nARTEX_VM_PROXY_PORT=8788\n' "$password" > "$ENV_FILE")
  unset password
  info "DB 비밀번호를 생성했습니다: .local-vm/compose.env (권한 600, 재실행 시 유지)"
fi
[ -f "$ENV_FILE" ] && [ -s "$ENV_FILE" ] || die "compose.env 가 비었거나 일반 파일이 아닙니다. 기존 비밀번호가 담긴 파일을 복원하세요."
chmod 600 "$ENV_FILE"
# 오류를 조기에 확인하되 비밀번호가 포함되는 렌더링 결과는 출력하지 않는다.
compose config --quiet

ready() {
  if ! compose up -d --no-build --pull never --wait --wait-timeout 180; then
    compose ps >&2 || true
    die "서비스가 준비되지 않았습니다. ./local-vm.sh logs 로 원인을 확인하세요. 설정과 볼륨은 보존했습니다."
  fi
  address=$(compose port artex 8787)
  info "준비 완료: http://$address"
  info "처음 접속하면 관리자 비밀번호와 LLM을 설정하세요. 점검 작업은 UI에서 직접 생성합니다."
}

case "$command_name" in
  up)
    info "현재 체크아웃의 프런트엔드와 Go 바이너리를 빌드합니다."
    compose build --pull artex
    compose pull postgres
    ready
    ;;
  build) compose build --pull artex ;;
  start) ready ;;
  stop) compose stop ;;
  down) compose down ;;
  status) compose ps ;;
  logs) compose logs --follow --tail=100 ;;
esac
