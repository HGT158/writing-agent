import { flushPromises, mount, type DOMWrapper } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  importFile: vi.fn(),
  importFolder: vi.fn(),
}))
vi.mock('../api/client', () => ({ apiClient: apiMocks }))

import ProjectExplorer from './ProjectExplorer.vue'
import type { Project, ProjectDocument } from '../types'

const project: Project = {
  project_id: 'project-1', assistant_id: 'default', name: '示例项目',
  root_path: 'managed/project-1', entry_document_id: 'd-root',
}

function doc(document_id: string, relative_path: string, editable = true): ProjectDocument {
  return {
    document_id, project_id: 'project-1', assistant_id: 'default',
    relative_path, version: 1, editable, content: null,
  }
}

function mountExplorer(tree: ProjectDocument[] = []) {
  return mount(ProjectExplorer, {
    props: {
      assistantId: 'default',
      projects: [project],
      activeProjectId: 'project-1',
      tree,
    },
  })
}

describe('ProjectExplorer', () => {
  it('renders documents nested under folder rows instead of a flat full-path list', async () => {
    const wrapper = mountExplorer([
      doc('d-root', 'article.md'),
      doc('d-ch1', 'chapters/ch01.md'),
      doc('d-ch2', 'chapters/ch02.md'),
      doc('d-note', 'assets/notes.md'),
    ])
    await flushPromises()

    const rows = wrapper.findAll('.file-tree .row-main')
    // VS Code 默认排序：同级文件与文件夹按名称交错，不把文件压到最下面；
    // 文件行只显示文件名并嵌套在所属文件夹下。
    expect(rows.map((row) => row.get('.file-label').text())).toEqual([
      'article.md', 'assets', 'notes.md', 'chapters', 'ch01.md', 'ch02.md',
    ])
    // 文件标签不再携带完整相对路径，避免子目录文档与根文档混淆。
    expect(rows.map((row) => row.get('.file-label').text()).join(' ')).not.toContain('/')
    const folderRows = wrapper.findAll('.file-tree .tree-row.folder-row .row-main')
    expect(folderRows.map((row) => row.get('.file-label').text())).toEqual(['assets', 'chapters'])
    // 文件行保留完整相对路径作为悬停提示。
    const chapterFile = rows.find((row) => row.get('.file-label').text() === 'ch01.md')
    expect(chapterFile?.attributes('title')).toBe('chapters/ch01.md')
    // 嵌套文件比所属文件夹缩进更深。
    const paddingLeft = (row: DOMWrapper<Element> | undefined) =>
      Number((row?.attributes('style') || '').match(/padding-left:\s*([\d.]+)px/)?.[1] ?? 0)
    expect(paddingLeft(chapterFile)).toBeGreaterThan(paddingLeft(folderRows[1]))
  })

  it('shows the active project files directly beneath it, pushing later projects down', async () => {
    const second: Project = {
      project_id: 'project-2', assistant_id: 'default', name: '第二个项目',
      root_path: 'managed/project-2', entry_document_id: 'd-root',
    }
    const wrapper = mount(ProjectExplorer, {
      props: {
        assistantId: 'default',
        projects: [project, second],
        activeProjectId: 'project-1',
        tree: [doc('d-root', 'article.md')],
      },
    })
    await flushPromises()

    // VS Code 多根目录行为：文件紧贴所属项目名下方，而不是垫在全部项目行之后。
    const labels = wrapper.findAll('.project-list .row-main')
      .map((row) => row.text().replace(/\s+/g, ''))
    expect(labels).toEqual(['⌄示例项目1', 'article.md', '›第二个项目'])
  })

  it('collapses and re-expands a folder by clicking its row', async () => {
    const wrapper = mountExplorer([
      doc('d-root', 'article.md'),
      doc('d-ch1', 'chapters/ch01.md'),
    ])
    await flushPromises()

    const folderRow = wrapper.findAll('.file-tree .row-main')
      .find((row) => row.get('.file-label').text() === 'chapters')
    await folderRow!.trigger('click')
    // 收起后 chapters 下方的内容上移，article.md 保持在项目名下方原位。
    expect(wrapper.findAll('.file-tree .row-main').map((row) => row.get('.file-label').text()))
      .toEqual(['article.md', 'chapters'])

    await wrapper.findAll('.file-tree .row-main')
      .find((row) => row.get('.file-label').text() === 'chapters')!.trigger('click')
    expect(wrapper.findAll('.file-tree .row-main').map((row) => row.get('.file-label').text()))
      .toEqual(['article.md', 'chapters', 'ch01.md'])
  })

  it('keeps readonly documents visible but disabled without row actions', async () => {
    const wrapper = mountExplorer([
      doc('d-ro', 'assets/data.txt', false),
    ])
    await flushPromises()

    const fileRow = wrapper.findAll('.file-tree .tree-row')
      .find((row) => row.get('.file-label').text() === 'data.txt')
    expect(fileRow?.get('.row-main').attributes('disabled')).toBeDefined()
    expect(fileRow?.find('.readonly-mark').exists()).toBe(true)
    // 只读文件不提供重命名/删除入口。
    expect(fileRow?.find('.row-actions').exists()).toBe(false)
  })

  it('emits openDocument when an editable file is clicked', async () => {
    const wrapper = mountExplorer([doc('d-ch1', 'chapters/ch01.md')])
    await flushPromises()

    await wrapper.findAll('.file-tree .row-main')
      .find((row) => row.get('.file-label').text() === 'ch01.md')!.trigger('click')

    expect(wrapper.emitted('openDocument')).toEqual([['project-1', 'd-ch1']])
  })

  it('exposes rename and delete actions on project and file rows', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mountExplorer([doc('d-ch1', 'chapters/ch01.md')])
    await flushPromises()

    const projectRow = wrapper.findAll('.project-list .tree-row')
      .find((row) => row.classes().includes('project-row'))!
    await projectRow.get('button[title="重命名项目"]').trigger('click')
    // 行内重命名：标签本身变成输入框（默认原名称），而不是挤在标签旁边。
    const projectInput = projectRow.get('input.rename-input')
    expect((projectInput.element as HTMLInputElement).value).toBe('示例项目')
    expect(projectRow.find('.row-main').exists()).toBe(false)
    await projectInput.setValue('改名项目')
    await projectInput.trigger('keydown.enter')
    expect(wrapper.emitted('renameProject')).toEqual([['project-1', '改名项目']])
    expect(projectRow.find('.row-main').exists()).toBe(true)

    await projectRow.get('button[title="删除项目（归档）"]').trigger('click')
    expect(wrapper.emitted('deleteProject')).toEqual([['project-1']])

    const fileRow = wrapper.findAll('.file-tree .tree-row')
      .find((row) => row.get('.file-label').text() === 'ch01.md')!
    await fileRow.get('button[title="重命名文件"]').trigger('click')
    const fileInput = fileRow.get('input.rename-input')
    // 默认值是原文件名（不含文件夹），聚焦后选中文件名主体（不含扩展名）。
    expect((fileInput.element as HTMLInputElement).value).toBe('ch01.md')
    expect((fileInput.element as HTMLInputElement).selectionEnd).toBe('ch01'.length)
    expect(fileRow.find('.row-main').exists()).toBe(false)
    await fileInput.setValue('chapter1.md')
    await fileInput.trigger('keydown.enter')
    // 文件重命名只编辑文件名，提交时保留所在文件夹。
    expect(wrapper.emitted('renameDocument')).toEqual([['project-1', 'd-ch1', 'chapters/chapter1.md']])

    await fileRow.get('button[title="删除文件"]').trigger('click')
    expect(wrapper.emitted('deleteDocument')).toEqual([['project-1', 'd-ch1']])
    vi.restoreAllMocks()
  })

  it('cancels inline rename with Esc and ignores empty names', async () => {
    const wrapper = mountExplorer([doc('d-root', 'article.md')])
    await flushPromises()

    const fileRow = wrapper.findAll('.file-tree .tree-row')
      .find((row) => row.get('.file-label').text() === 'article.md')!
    await fileRow.get('button[title="重命名文件"]').trigger('click')
    let input = fileRow.get('input.rename-input')
    await input.setValue('新名字.md')
    await input.trigger('keydown.esc')
    expect(wrapper.emitted('renameDocument')).toBeUndefined()
    expect(fileRow.find('input.rename-input').exists()).toBe(false)

    await fileRow.get('button[title="重命名文件"]').trigger('click')
    input = fileRow.get('input.rename-input')
    await input.setValue('   ')
    await input.trigger('keydown.enter')
    expect(wrapper.emitted('renameDocument')).toBeUndefined()
  })
})
