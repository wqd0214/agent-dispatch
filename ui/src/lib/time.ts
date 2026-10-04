// 相对时间与耗时的纯文本格式化

const MINUTE = 60
const HOUR = 3600
const DAY = 86400

/** 秒数 → 「7 分 31 秒」「2 小时 3 分」这类耗时文字；空值给 '-' */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || isNaN(seconds) || seconds < 0) return '-'
  const s = Math.floor(seconds)
  if (s < 60) return `${s} 秒`
  if (s < HOUR) {
    const m = Math.floor(s / MINUTE)
    const rem = s % MINUTE
    return rem ? `${m} 分 ${rem} 秒` : `${m} 分`
  }
  const h = Math.floor(s / HOUR)
  const m = Math.floor((s % HOUR) / MINUTE)
  return m ? `${h} 小时 ${m} 分` : `${h} 小时`
}

/** 「2026-10-04 12:51:20」这类本地时间字符串 → 距 now 的相对时间；解析失败或未来时间给原串 */
export function relativeTime(stamp: string | null | undefined, now: number = Date.now()): string {
  if (!stamp) return '-'
  const ts = Date.parse(stamp.replace(' ', 'T'))
  if (isNaN(ts)) return stamp
  const diff = Math.floor((now - ts) / 1000)
  if (diff < 0) return stamp
  if (diff < 10) return '刚刚'
  if (diff < MINUTE) return `${diff} 秒前`
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)} 分钟前`
  if (diff < DAY) return `${Math.floor(diff / HOUR)} 小时前`
  if (diff < DAY * 30) return `${Math.floor(diff / DAY)} 天前`
  return stamp
}

/** 作业列表的时长列：运行中给「14 分」且随刷新走，已结束给总耗时 */
export function jobTimeText(job: { state: string; seconds: number | null }): string {
  return job.state === 'running' && job.seconds != null ? formatDuration(job.seconds) : formatDuration(job.seconds)
}
