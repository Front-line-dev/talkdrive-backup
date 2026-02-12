import os
import re
import csv
import requests
import json
import threading
from datetime import datetime
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BACKUP_PATH = './backups'
COOKIE_FILE = 'talkcloud.kakao.com_cookies.txt'
HISTORY_FILE = os.path.join(BACKUP_PATH, 'download_history.csv')
FETCH_COUNT = 100
THREADS_COUNT = 5
MAX_SIZE_BYTES = 5 * 1024 * 1024 * 1024  # 5GB
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
    backoff_factor=1,  # 1초, 2초, 4초 간격으로 재시도
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
    """폴더 내 같은 날짜 파일들을 확인해서 다음 순번 반환"""
    if not os.path.exists(folder):
        return 1
    pattern = re.compile(rf'^{date_str}_(\d{{3}})\.')
    max_seq = 0
    for f in os.listdir(folder):
        m = pattern.match(f)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def request_list():
    url = f'https://drawer-api.kakao.com/mediaFile/list?verticalType=MEDIA&fetchCount={FETCH_COUNT}&joined=true&direction=ASC'
    response = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT_BASE))
    response.raise_for_status()
    return response.json()


def download_item(item, results, index):
    """단일 항목 다운로드 (청크 방식 + 재시도)"""
    chat_id = item['hashedChatId'][:8]
    created = datetime.fromtimestamp(int(item['createdAt']) / 1000)
    date_str = created.strftime('%Y%m%d')
    ext = item['extension']
    expected_size = int(item['size'])
    content_type = item.get('contentType', '')

    folder = os.path.join(BACKUP_PATH, f"chat_{chat_id}")
    os.makedirs(folder, exist_ok=True)

    # 순번 결정
    with seq_lock:
        key = (chat_id, date_str)
        seq = seq_counters.get(key, get_next_seq(folder, date_str))
        seq_counters[key] = seq + 1

    filename = f"{date_str}_{seq:03d}.{ext}"
    filepath = os.path.join(folder, filename)

    # 재시도 루프
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            read_timeout = READ_TIMEOUT_BASE + (expected_size / (1024 * 1024)) * READ_TIMEOUT_PER_MB
            response = session.get(f"{item['url']}?attach", timeout=(CONNECT_TIMEOUT, read_timeout), stream=True)
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}")

            # 청크 방식으로 저장
            downloaded = 0
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):  # 1MB 청크
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
                # 실패한 임시 파일 정리
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


def request_delete(ids):
    data = {"verticalType": "MEDIA", "ids": ids}
    response = session.post(
        'https://drawer-api.kakao.com/mediaFile/delete',
        data=json.dumps(data),
        headers={'Content-Type': 'application/json'},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT_BASE),
    )
    return response.status_code == 204


# 메인 루프
batch = 0
total_downloaded = 0
total_skipped = 0
total_deleted = 0
total_failed = 0
total_bytes = 0
seq_lock = threading.Lock()
seq_counters = {}

while True:
    batch += 1

    # 용량 제한 체크
    if total_bytes >= MAX_SIZE_BYTES:
        print(f"\n용량 제한 도달 ({total_bytes / (1024**3):.2f} GB). 중단합니다.")
        break

    # 목록 요청
    try:
        file_list = request_list()
    except Exception as e:
        print(f"\n목록 요청 실패: {e}")
        break

    total_count = file_list.get('totalCount', 0)
    items = file_list.get('items', [])

    if total_count == 0 or len(items) == 0:
        print("\n모든 사진 처리 완료!")
        break

    # 이미 다운로드된 항목과 새 항목 분리
    new_items = [item for item in items if item['id'] not in downloaded_ids]
    skip_items = [item for item in items if item['id'] in downloaded_ids]

    print(f"\n===== 배치 {batch} | 남은 사진: {total_count}개 | 신규: {len(new_items)}개 | 기다운로드: {len(skip_items)}개 | 누적: {total_bytes / (1024**3):.2f} GB =====")

    # 신규 항목 다운로드
    results = []
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

    # 삭제 대상: 새로 다운로드 성공한 것 + 이전에 이미 받은 것
    delete_ids = [item['id'] for item in skip_items]
    if results:
        delete_ids += [r['id'] for r in results if r and r['success']]

    total_skipped += len(skip_items)
    if skip_items:
        print(f"  기다운로드: {len(skip_items)}개 (다운로드 생략, 삭제만 진행)")

    if delete_ids:
        if request_delete(delete_ids):
            total_deleted += len(delete_ids)
            print(f"  서버 삭제 완료 ({len(delete_ids)}개)")
        else:
            print("  서버 삭제 실패! 다음 실행 시 재시도됩니다.")

print(f"\n===== 최종 결과 =====")
print(f"다운로드 성공: {total_downloaded}개 ({total_bytes / (1024**3):.2f} GB)")
print(f"기다운로드 (다운로드 생략): {total_skipped}개")
print(f"서버 삭제: {total_deleted}개")
print(f"실패: {total_failed}개")
