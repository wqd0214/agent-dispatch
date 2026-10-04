// 日志增量拼接的纯逻辑：offset 乱序或回退时重置

export interface LogState { offset: number; text: string }

/** 把一次增量响应并进当前日志。offset 不匹配（回退 / 跳变）时整段替换为新内容。 */
export function appendLog(state: LogState, chunk: { offset: number; text: string }): LogState {
  if (state.offset !== chunk.offset - chunk.text.length) {
    // 服务端从更靠前的位置重新发了一段：直接替换，避免重复
    return { offset: chunk.offset, text: chunk.text }
  }
  return { offset: chunk.offset, text: state.text + chunk.text }
}
