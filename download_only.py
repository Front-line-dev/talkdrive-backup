import os
import re
import sys
import csv

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
import requests
import json
import threading
from datetime import datetime
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VALID_TYPES = ['MEDIA', 'FILE', 'LINK']
VERTICAL_TYPE = sys.argv[1].upper() if len(sys.argv) > 1 else 'MEDIA'
if VERTICAL_TYPE not in VALID_TYPES:
    print(f"잘못된 타입: {VERTICAL_TYPE}. 사용 가능: {', '.join(VALID_TYPES)}")
    sys.exit(1)
print(f"타입: {VERTICAL_TYPE}")

BACKUP_PATH = './backups'
COOKIE_FILE = 'talkcloud.kakao.com_cookies.txt'
HISTORY_FILE = os.path.join(BACKUP_PATH, 'download_history.csv')

# 커서 파일: 타입별 분리 (기존 download_cursor.txt → download_cursor_MEDIA.txt 마이그레이션)
def _get_cursor_file():
    if VERTICAL_TYPE == 'MEDIA':
        old_path = os.path.join(BACKUP_PATH, 'download_cursor.txt')
        new_path = os.path.join(BACKUP_PATH, 'download_cursor_MEDIA.txt')
        if os.path.exists(old_path) and not os.path.exists(new_path):
            os.rename(old_path, new_path)
        return new_path
    return os.path.join(BACKUP_PATH, f'download_cursor_{VERTICAL_TYPE}.txt')

CURSOR_FILE = _get_cursor_file()
FETCH_COUNT = 100
THREADS_COUNT = 5
MAX_SIZE_BYTES = 5 * 1024 * 1024 * 1024  # 5GB
MAX_COUNT = None  # 개수 제한 (None이면 제한 없음)
MAX_RETRIES = 3
CONNECT_TIMEOUT = 3  # 연결 타임아웃 (초)
READ_TIMEOUT_BASE = 3  # 읽기 타임아웃 기본값 (초)
READ_TIMEOUT_PER_MB = 2  # MB당 추가 타임아웃 (초)

CSV_FIELDS = ['id', 'chatId', 'date', 'filename', 'size', 'contentType', 'status', 'downloadedAt']

REQ_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
    'Accept': 'application/json+javascript'
}

os.makedirs(BACKUP_PATH, exist_ok=True)

# 다운로드 이력 로드
downloaded_ids = set()
if os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, 'r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            downloaded_ids.add(row['id'])
else:
    with open(HISTORY_FILE, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
print(f"다운로드 이력: {len(downloaded_ids)}개 로드됨")

# 저장된 커서 로드 (이전 실행 위치에서 재개)
saved_cursor = None
if os.path.exists(CURSOR_FILE):
    with open(CURSOR_FILE, 'r', encoding='utf-8') as f:
        saved_cursor = f.read().strip() or None
    if saved_cursor:
        print(f"이전 커서에서 재개: {saved_cursor}")

# 쿠키 읽기
cookies = {}
with open(COOKIE_FILE, 'r', encoding='utf-8') as file:
    for line in file:
        line = line.strip()
        if line and not line.startswith('#'):
            parts = line.split('\t')
            if len(parts) >= 7:
                cookies[parts[5].strip()] = parts[6].strip()

print(f"쿠키 {len(cookies)}개 로드됨")

# 세션 설정 (연결 재사용 + 자동 재시도)
session = requests.Session()
retry_strategy = Retry(
    total=MAX_RETRIES,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=THREADS_COUNT,
    pool_maxsize=THREADS_COUNT,
)
session.mount('https://', adapter)
session.headers.update(REQ_HEADERS)
session.cookies.update(cookies)


def get_next_seq(folder, date_str):
    if not os.path.exists(folder):
        return 1
    pattern = re.compile(rf'^{date_str}_(\d{{3}})\.')
    max_seq = 0
    for f in os.listdir(folder):
        m = pattern.match(f)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def get_output_folder(chat_id):
    if VERTICAL_TYPE == 'MEDIA':
        return os.path.join(BACKUP_PATH, f"chat_{chat_id}")
    elif VERTICAL_TYPE == 'FILE':
        return os.path.join(BACKUP_PATH, 'files', f"chat_{chat_id}")
    elif VERTICAL_TYPE == 'LINK':
        return os.path.join(BACKUP_PATH, 'links', f"chat_{chat_id}")


def sanitize_filename(name):
    name = re.sub(r'[\\/*?:"<>|]', '_', name)
    name = name.strip(' .')
    if len(name) > 240:
        base, ext = os.path.splitext(name)
        name = base[:240 - len(ext)] + ext
    return name or 'unnamed'


def get_unique_filepath(folder, filename):
    filepath = os.path.join(folder, filename)
    if not os.path.exists(filepath):
        return filepath, filename
    base, ext = os.path.splitext(filename)
    counter = 1
    while True:
        new_name = f"{base} ({counter}){ext}"
        new_path = os.path.join(folder, new_name)
        if not os.path.exists(new_path):
            return new_path, new_name
        counter += 1


def request_list(cursor=None):
    url = f'https://drawer-api.kakao.com/mediaFile/list?verticalType={VERTICAL_TYPE}&fetchCount={FETCH_COUNT}&joined=true&direction=ASC'
    if cursor:
        url += f'&cursor={cursor}'
    response = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT_BASE))
    response.raise_for_status()
    return response.json()


def download_item(item, results, index):
    chat_id = item['hashedChatId'][:8]
    created = datetime.fromtimestamp(int(item['createdAt']) / 1000)
    date_str = created.strftime('%Y%m%d')
    ext = item['extension']
    expected_size = int(item['size'])
    content_type = item.get('contentType', '')

    folder = get_output_folder(chat_id)
    os.makedirs(folder, exist_ok=True)

    if VERTICAL_TYPE == 'FILE' and item.get('originalFileName'):
        with seq_lock:
            raw_name = sanitize_filename(item['originalFileName'])
            filepath, filename = get_unique_filepath(folder, raw_name)
    else:
        with seq_lock:
            key = (chat_id, date_str)
            seq = seq_counters.get(key, get_next_seq(folder, date_str))
            seq_counters[key] = seq + 1
        filename = f"{date_str}_{seq:03d}.{ext}"
        filepath = os.path.join(folder, filename)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            read_timeout = READ_TIMEOUT_BASE + (expected_size / (1024 * 1024)) * READ_TIMEOUT_PER_MB
            response = session.get(f"{item['url']}?attach", timeout=(CONNECT_TIMEOUT, read_timeout), stream=True)
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}")

            downloaded = 0
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)

            # 0바이트 체크 (메타데이터 size는 서버 변환으로 실제와 다를 수 있어 신뢰하지 않음)
            if downloaded == 0:
                os.remove(filepath)
                raise Exception("빈 파일 (0 bytes)")

            results[index] = {
                'success': True, 'id': item['id'], 'path': filepath, 'size': downloaded,
                'chatId': chat_id, 'date': date_str, 'filename': filename, 'contentType': content_type,
            }
            print(f"  OK  {filepath} ({downloaded:,} bytes)")
            return

        except Exception as e:
            if attempt < MAX_RETRIES:
                print(f"  재시도 {attempt}/{MAX_RETRIES} {filename}: {e}")
            else:
                if os.path.exists(filepath):
                    os.remove(filepath)
                results[index] = {'success': False, 'id': item['id'], 'error': str(e)}
                print(f"  FAIL {filename}: {e}")


def save_history(results_list):
    """다운로드 결과를 CSV에 추가 (성공/실패 모두)"""
    with open(HISTORY_FILE, 'a', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for r in results_list:
            if r['success']:
                writer.writerow({
                    'id': r['id'], 'chatId': r['chatId'], 'date': r['date'],
                    'filename': r['filename'], 'size': r['size'], 'contentType': r['contentType'],
                    'status': 'OK', 'downloadedAt': now,
                })
            else:
                writer.writerow({
                    'id': r['id'], 'chatId': '', 'date': '', 'filename': '',
                    'size': 0, 'contentType': '', 'status': f"FAIL: {r['error']}",
                    'downloadedAt': now,
                })
            downloaded_ids.add(r['id'])


# 메인 루프
batch = 0
total_downloaded = 0
total_skipped = 0
total_failed = 0
total_bytes = 0
cursor = saved_cursor
seq_lock = threading.Lock()
seq_counters = {}

while True:
    batch += 1

    if total_bytes >= MAX_SIZE_BYTES:
        print(f"\n용량 제한 도달 ({total_bytes / (1024**3):.2f} GB). 중단합니다.")
        print(f"다음 실행 시 커서 {cursor}에서 재개됩니다.")
        break

    if MAX_COUNT and total_downloaded >= MAX_COUNT:
        print(f"\n개수 제한 도달 ({total_downloaded}개). 중단합니다.")
        print(f"다음 실행 시 커서 {cursor}에서 재개됩니다.")
        break

    try:
        file_list = request_list(cursor)
    except Exception as e:
        print(f"\n목록 요청 실패: {e}")
        break

    total_count = file_list.get('totalCount', 0)
    items = file_list.get('items', [])
    has_more = file_list.get('hasMore', False)

    if total_count == 0 or len(items) == 0:
        if os.path.exists(CURSOR_FILE):
            os.remove(CURSOR_FILE)
        print(f"\n모든 {VERTICAL_TYPE} 처리 완료!")
        break

    # 다음 배치를 위한 cursor 설정 + 저장
    if has_more and items:
        cursor = items[-1]['id']
        with open(CURSOR_FILE, 'w', encoding='utf-8') as f:
            f.write(cursor)

    # 이미 다운로드된 항목 분리
    new_items = [item for item in items if item['id'] not in downloaded_ids]
    skip_count = len(items) - len(new_items)
    total_skipped += skip_count

    print(f"\n===== 배치 {batch} | 전체 {VERTICAL_TYPE}: {total_count}개 | 신규: {len(new_items)}개 | 기다운로드: {skip_count}개 | 누적: {total_bytes / (1024**3):.2f} GB =====")

    if new_items:
        results = [None] * len(new_items)
        threads = []

        for i, item in enumerate(new_items):
            thread = threading.Thread(target=download_item, args=(item, results, i))
            threads.append(thread)
            thread.start()
            if len(threads) >= THREADS_COUNT:
                for t in threads:
                    t.join()
                threads = []

        for t in threads:
            t.join()

        success = [r for r in results if r and r['success']]
        failed = [r for r in results if r and not r['success']]

        batch_bytes = sum(r['size'] for r in success)
        total_bytes += batch_bytes
        total_downloaded += len(success)
        total_failed += len(failed)

        # CSV에 이력 기록 (성공 + 실패 모두)
        save_history([r for r in results if r])

        print(f"\n  다운로드: 성공 {len(success)}개 ({batch_bytes / (1024**2):.1f} MB) / 실패 {len(failed)}개")

        if failed:
            print("  실패 항목:")
            for f in failed:
                print(f"    - {f['id']}: {f['error']}")
    else:
        print("  이번 배치 전부 기다운로드")

    if not has_more:
        # 모든 파일 처리 완료 → 커서 파일 삭제 (다음 실행 시 처음부터)
        if os.path.exists(CURSOR_FILE):
            os.remove(CURSOR_FILE)
        print(f"\n모든 {VERTICAL_TYPE} 처리 완료!")
        break

print(f"\n===== 최종 결과 =====")
print(f"다운로드 성공: {total_downloaded}개 ({total_bytes / (1024**3):.2f} GB)")
print(f"기다운로드 (다운로드 생략): {total_skipped}개")
print(f"실패: {total_failed}개")
print("(서버에서 삭제하지 않았습니다)")
