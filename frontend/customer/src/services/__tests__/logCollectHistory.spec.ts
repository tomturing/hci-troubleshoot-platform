import { describe, expect, it } from 'vitest'
import { pickDisplayTask } from '../logCollectHistory'

describe('pickDisplayTask', () => {
  // 现场回归（Q2026102701193，2026-10-27）：用户 16:40 新建的 P2 任务 failed，
  // 结果页却展示 10:03 创建、18:05 完成的那条 P1 任务（"已由插件直传"）。
  // 用户看到交付路径是 P1，以为系统判错了路径；实际是展示选错了记录。
  it('最新一条失败记录必须优先于更早的完成记录', () => {
    const items = [
      { status: 'failed', created_at: '2026-10-27T16:40:06+08:00', finished_at: '2026-10-27T16:45:40+08:00' },
      { status: 'completed', created_at: '2026-10-27T10:03:50+08:00', finished_at: '2026-10-27T18:05:00+08:00' },
    ]
    expect(pickDisplayTask(items)?.status).toBe('failed')
  })

  // 长耗时采集是常态（现场那条跑了 8 小时 36 分，18:05 才结束）：
  // 若按结束时间排，它会顶掉用户 16:40 刚发起、16:45 就失败的那次尝试，
  // 用户打开面板看到"已完成"，会以为自己刚选的 P2 成功了。
  it('按发起时间判定"这次"，长采集结束晚也不许顶掉刚发起的尝试', () => {
    const items = [
      { status: 'completed', created_at: '2026-10-27T10:03:00+08:00', finished_at: '2026-10-27T18:05:00+08:00' },
      { status: 'failed', created_at: '2026-10-27T16:40:00+08:00', finished_at: '2026-10-27T16:45:00+08:00' },
    ]
    expect(pickDisplayTask(items)?.status).toBe('failed')
  })

  it('不依赖入参顺序（后端换排序策略时不能挑错）', () => {
    const newer = { status: 'failed', created_at: '2026-10-27T16:40:00+08:00', finished_at: '2026-10-27T16:45:00+08:00' }
    const older = { status: 'completed', created_at: '2026-10-27T10:00:00+08:00', finished_at: '2026-10-27T10:30:00+08:00' }
    expect(pickDisplayTask([older, newer])?.status).toBe('failed')
    expect(pickDisplayTask([newer, older])?.status).toBe('failed')
  })

  it('只有进行中的任务时返回 null（交给接管流程）', () => {
    expect(pickDisplayTask([{ status: 'collecting', created_at: '2026-10-27T16:40:00+08:00' }])).toBeNull()
    expect(pickDisplayTask([])).toBeNull()
  })

  it('缺时间字段时不炸，稳定返回第一条终态', () => {
    const items = [{ status: 'failed' }, { status: 'completed' }]
    expect(pickDisplayTask(items)?.status).toBe('failed')
  })
})
