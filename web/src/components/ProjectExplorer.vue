<script setup lang="ts">
import { computed, ref } from 'vue'
import { FilePlus2, FolderArchive, FolderPlus, Pencil, Trash2, Upload } from '@lucide/vue'

import { apiClient } from '../api/client'
import type { Project, ProjectDocument } from '../types'

const props = defineProps<{
  assistantId: string
  projects: Project[]
  activeProjectId: string | null
  tree: ProjectDocument[]
}>()

const emit = defineEmits<{
  selectProject: [projectId: string]
  openDocument: [projectId: string, documentId: string]
  imported: [projectId: string]
  createProject: []
  renameProject: [projectId: string, name: string]
  deleteProject: [projectId: string]
  renameDocument: [projectId: string, documentId: string, relativePath: string]
  deleteDocument: [projectId: string, documentId: string]
}>()

const fileInput = ref<HTMLInputElement>()
const folderInput = ref<HTMLInputElement>()
const importBusy = ref(false)
const importError = ref('')
const editableTree = computed(() => props.tree.filter((item) => item.editable))
const collapsedFolders = ref(new Set<string>())
const renaming = ref<{ kind: 'project' | 'file'; id: string; value: string } | null>(null)

/** 行内重命名挂载后聚焦并选中名称主体（不含扩展名），对齐 VS Code 体验。 */
const vFocus = {
  mounted: (el: HTMLInputElement) => {
    el.focus()
    const dot = el.value.lastIndexOf('.')
    el.setSelectionRange(0, dot > 0 ? dot : el.value.length)
  },
}

interface TreeFolder {
  kind: 'folder'
  name: string
  path: string
  children: TreeNode[]
}
interface TreeFile {
  kind: 'file'
  name: string
  doc: ProjectDocument
}
type TreeNode = TreeFolder | TreeFile

interface DisplayRow {
  key: string
  kind: 'folder' | 'file'
  label: string
  depth: number
  expanded: boolean
  path: string
  doc?: ProjectDocument
}

/**
 * 把平铺的 relative_path 列表重建成 VS Code 式嵌套树：同级文件与文件夹
 * 按名称交错排序（VS Code 默认，不把文件压到最下面）；文件行只显示文件名，
 * 完整相对路径作为悬停提示。折叠状态按文件夹路径记录，默认全部展开。
 */
const treeRows = computed<DisplayRow[]>(() => {
  const root: TreeFolder = { kind: 'folder', name: '', path: '', children: [] }
  const folders = new Map<string, TreeFolder>([['', root]])
  const ensureFolder = (path: string): TreeFolder => {
    const existing = folders.get(path)
    if (existing) return existing
    const slash = path.lastIndexOf('/')
    const folder: TreeFolder = {
      kind: 'folder',
      name: path.slice(slash + 1),
      path,
      children: [],
    }
    ensureFolder(slash < 0 ? '' : path.slice(0, slash)).children.push(folder)
    folders.set(path, folder)
    return folder
  }
  for (const item of props.tree) {
    const slash = item.relative_path.lastIndexOf('/')
    ensureFolder(slash < 0 ? '' : item.relative_path.slice(0, slash)).children.push({
      kind: 'file',
      name: item.relative_path.slice(slash + 1),
      doc: item,
    })
  }
  const sortSiblings = (a: TreeNode, b: TreeNode) => a.name.localeCompare(b.name, 'zh-CN')
  const rows: DisplayRow[] = []
  const walk = (node: TreeNode, depth: number) => {
    if (node.kind === 'folder') {
      const expanded = !collapsedFolders.value.has(node.path)
      rows.push({
        key: `folder:${node.path}`,
        kind: 'folder',
        label: node.name,
        depth,
        expanded,
        path: node.path,
      })
      if (expanded) node.children.sort(sortSiblings).forEach((child) => walk(child, depth + 1))
      return
    }
    rows.push({
      key: `file:${node.doc.document_id}`,
      kind: 'file',
      label: node.name,
      depth,
      expanded: false,
      path: node.doc.relative_path,
      doc: node.doc,
    })
  }
  root.children.sort(sortSiblings).forEach((child) => walk(child, 0))
  return rows
})

function toggleFolder(path: string) {
  const collapsed = collapsedFolders.value
  if (collapsed.has(path)) collapsed.delete(path)
  else collapsed.add(path)
}

function startRenameProject(project: Project) {
  renaming.value = { kind: 'project', id: project.project_id, value: project.name }
}

function startRenameFile(row: DisplayRow) {
  if (!row.doc) return
  renaming.value = { kind: 'file', id: row.doc.document_id, value: row.label }
}

/** 行内重命名提交：文件只编辑文件名，提交时拼回所在文件夹（架构 §5.10 v1.25）。 */
function commitRename() {
  const target = renaming.value
  renaming.value = null
  const value = target?.value.trim()
  if (!target || !value) return
  if (target.kind === 'project') {
    emit('renameProject', target.id, value)
    return
  }
  const document = props.tree.find((item) => item.document_id === target.id)
  if (!document || !props.activeProjectId) return
  const folder = document.relative_path.includes('/')
    ? document.relative_path.slice(0, document.relative_path.lastIndexOf('/') + 1)
    : ''
  emit('renameDocument', props.activeProjectId, target.id, `${folder}${value}`)
}

function cancelRename() {
  renaming.value = null
}

function confirmDeleteProject(project: Project) {
  if (window.confirm(`确定删除项目「${project.name}」吗？项目目录会归档到 data/archive，可手动恢复。`)) {
    emit('deleteProject', project.project_id)
  }
}

function confirmDeleteDocument(row: DisplayRow) {
  const document = row.doc
  if (!document) return
  if (window.confirm(`确定删除文件「${document.relative_path}」吗？此操作不可恢复。`)) {
    emit('deleteDocument', props.activeProjectId!, document.document_id)
  }
}

async function importFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  importBusy.value = true
  importError.value = ''
  try {
    const project = await apiClient.importFile(props.assistantId, file)
    emit('imported', project.project_id)
  } catch (error) {
    importError.value = error instanceof Error ? error.message : String(error)
  } finally {
    importBusy.value = false
    input.value = ''
  }
}

async function importFolder(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  if (!files.length) return
  const firstPath = (files[0] as File & { webkitRelativePath?: string }).webkitRelativePath || files[0].name
  const name = firstPath.split('/')[0] || files[0].name
  importBusy.value = true
  importError.value = ''
  try {
    const project = await apiClient.importFolder(props.assistantId, name, files)
    emit('imported', project.project_id)
  } catch (error) {
    importError.value = error instanceof Error ? error.message : String(error)
  } finally {
    importBusy.value = false
    input.value = ''
  }
}
</script>

<template>
  <aside class="explorer-panel">
    <div class="panel-heading">
      <span>项目</span>
      <div class="heading-actions">
        <button title="新建空白项目" :disabled="importBusy" @click="emit('createProject')"><FilePlus2 :size="15" /></button>
        <button title="导入文本文件" :disabled="importBusy" @click="fileInput?.click()"><Upload :size="15" /></button>
        <button title="导入文件夹" :disabled="importBusy" @click="folderInput?.click()"><FolderPlus :size="15" /></button>
      </div>
    </div>
    <input ref="fileInput" class="visually-hidden" type="file" accept=".md,.markdown,.txt" @change="importFile" />
    <input ref="folderInput" class="visually-hidden" type="file" multiple webkitdirectory directory @change="importFolder" />
    <p v-if="importError" class="inline-error">{{ importError }}</p>
    <div class="project-list">
      <template v-for="project in projects" :key="project.project_id">
        <div
          class="tree-row project-row"
          :class="{ selected: project.project_id === activeProjectId }"
        >
          <input
            v-if="renaming?.kind === 'project' && renaming.id === project.project_id"
            v-model="renaming.value"
            v-focus
            class="rename-input"
            type="text"
            @keydown.enter.prevent="commitRename"
            @keydown.esc="cancelRename"
          />
          <button v-else class="row-main" @click="emit('selectProject', project.project_id)">
            <span class="tree-chevron">{{ project.project_id === activeProjectId ? '⌄' : '›' }}</span>
            <span class="project-name">{{ project.name }}</span>
            <span v-if="project.project_id === activeProjectId" class="project-count">{{ editableTree.length }}</span>
          </button>
          <span v-if="renaming?.kind !== 'project' || renaming.id !== project.project_id" class="row-actions">
            <button class="row-action" title="重命名项目" @click.stop="startRenameProject(project)"><Pencil :size="13" /></button>
            <button class="row-action" title="删除项目（归档）" @click.stop="confirmDeleteProject(project)"><Trash2 :size="13" /></button>
          </span>
        </div>
        <!-- 活动项目的文件树紧贴该项目行渲染，把后面的项目行推下去
             （VS Code 多根目录行为），不再统一垫在全部项目行之后。 -->
        <div v-if="project.project_id === activeProjectId" class="file-tree">
          <div
            v-for="row in treeRows"
            :key="row.key"
            class="tree-row"
            :class="row.kind === 'folder' ? 'folder-row' : 'file-row'"
          >
            <input
              v-if="renaming?.kind === 'file' && row.doc && renaming.id === row.doc.document_id"
              v-model="renaming.value"
              v-focus
              class="rename-input"
              type="text"
              :style="{ marginLeft: `${8 + row.depth * 14}px` }"
              @keydown.enter.prevent="commitRename"
              @keydown.esc="cancelRename"
            />
            <button
              v-else
              class="row-main"
              :style="{ paddingLeft: `${8 + row.depth * 14}px` }"
              :disabled="row.kind === 'file' && !row.doc!.editable"
              :title="row.kind === 'file' ? row.doc!.relative_path : row.path"
              @click="row.kind === 'folder' ? toggleFolder(row.path) : emit('openDocument', activeProjectId!, row.doc!.document_id)"
            >
              <span v-if="row.kind === 'folder'" class="tree-chevron">{{ row.expanded ? '⌄' : '›' }}</span>
              <span v-else class="tree-chevron" aria-hidden="true"></span>
              <span class="file-label">{{ row.label }}</span>
              <span v-if="row.kind === 'file' && !row.doc!.editable" class="readonly-mark" title="只读文件"><FolderArchive :size="13" /></span>
            </button>
            <span
              v-if="!(renaming?.kind === 'file' && row.doc && renaming.id === row.doc.document_id)
                && row.kind === 'file' && row.doc!.editable"
              class="row-actions"
            >
              <button class="row-action" title="重命名文件" @click.stop="startRenameFile(row)"><Pencil :size="13" /></button>
              <button class="row-action" title="删除文件" @click.stop="confirmDeleteDocument(row)"><Trash2 :size="13" /></button>
            </span>
          </div>
          <p v-if="!tree.length" class="empty-hint">项目为空</p>
        </div>
      </template>
    </div>
    <p v-if="!projects.length" class="empty-hint">新建或导入第一个项目</p>
  </aside>
</template>
