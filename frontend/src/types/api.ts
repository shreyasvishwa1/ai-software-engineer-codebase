// Types mirroring the backend's /api/v1 response schemas.

export type RepositoryStatus = 'pending' | 'processing' | 'ready' | 'failed'
export type RepositorySourceType = 'zip' | 'github'
export type AnalysisJobStatus = 'pending' | 'processing' | 'completed' | 'failed'
export type AnalysisJobType = 'ingestion' | 'code_metrics'

export interface User {
  id: string
  email: string
  created_at: string
  updated_at: string
}

export interface TokenResponse {
  access_token: string
  token_type: 'bearer'
}

export interface Project {
  id: string
  name: string
  description: string | null
  created_at: string
  updated_at: string
}

export interface ProjectCreate {
  name: string
  description?: string | null
}

export interface Repository {
  id: string
  project_id: string
  name: string
  source_type: RepositorySourceType
  source_url: string | null
  default_branch: string | null
  status: RepositoryStatus
  created_at: string
  updated_at: string
}

export interface RepositoryCreate {
  name: string
  source_type: RepositorySourceType
  source_url?: string
  default_branch?: string
}

export interface AnalysisJob {
  id: string
  project_id: string
  repository_id: string
  job_type: AnalysisJobType
  status: AnalysisJobStatus
  progress: number
  error_message: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface IngestionJobResponse {
  job_id: string
  repository_id: string
  status: AnalysisJobStatus
}

export interface TreeFile {
  type: 'file'
  name: string
  path: string
  extension: string | null
  language: string | null
  size_bytes: number
}

export interface TreeDirectory {
  type: 'directory'
  name: string
  path: string
  children: TreeNode[]
}

export type TreeNode = TreeDirectory | TreeFile

export interface RepositoryTree {
  repository_id: string
  status: RepositoryStatus
  file_count: number
  total_bytes: number
  tree: TreeDirectory
}

export interface FileContent {
  path: string
  language: string | null
  size_bytes: number
  /** null when the file cannot be shown as text; see reason */
  content: string | null
  reason: 'binary' | 'too_large' | null
}

export type FileMetricStatus = 'analyzed' | 'binary' | 'too_large' | 'unavailable'

export interface FileMetric {
  path: string
  language: string | null
  status: FileMetricStatus
  /** set only for analyzed files */
  line_count: number | null
  blank_line_count: number | null
}

export interface CodeMetricsSummary {
  files: number
  analyzed: number
  binary: number
  too_large: number
  unavailable: number
  total_lines: number
  blank_lines: number
}

export interface CodeMetrics {
  /** the latest code-metrics job, if any */
  job: AnalysisJob | null
  /** null until the current snapshot is analyzed */
  summary: CodeMetricsSummary | null
  files: FileMetric[]
}

export interface CodeMetricsJob {
  job_id: string
  repository_id: string
  status: AnalysisJobStatus
}

export interface FileExplanation {
  path: string
  /** plain text from the model; untrusted, render as text only */
  explanation: string
  model: string
  input_tokens: number | null
  output_tokens: number | null
}

export type ManifestStatus = 'parsed' | 'invalid' | 'too_large' | 'unavailable'

export interface DependencyEntry {
  name: string
  /** extras, version constraints and markers exactly as written */
  specifier: string
  /** runtime | dev | peer | optional | optional:<group> */
  scope: string
}

export interface DependencyManifest {
  path: string
  ecosystem: 'npm' | 'python'
  status: ManifestStatus
  dependencies: DependencyEntry[]
}

export interface DependencySummary {
  manifests: number
  parsed: number
  invalid: number
  too_large: number
  unavailable: number
  dependencies: number
}

export interface DependencyInventory {
  manifests: DependencyManifest[]
  summary: DependencySummary
  truncated: boolean
}

export type SecretRule =
  | 'aws-access-key-id'
  | 'github-token'
  | 'slack-token'
  | 'stripe-live-key'
  | 'google-api-key'
  | 'anthropic-api-key'
  | 'private-key'

/** Location of a likely secret; the server never sends the matched value. */
export interface SecretFinding {
  /** POSIX path relative to the repository root */
  path: string
  /** 1-based */
  line: number
  rule: SecretRule
  description: string
}

export interface SecretScanSummary {
  files_scanned: number
  /** includes content that is not valid UTF-8 */
  binary: number
  too_large: number
  unavailable: number
  findings: number
}

export interface SecretScan {
  findings: SecretFinding[]
  summary: SecretScanSummary
  truncated: boolean
}

/** One matching line from the server's literal, case-insensitive search. */
export interface CodeSearchMatch {
  /** POSIX path relative to the repository root */
  path: string
  /** 1-based */
  line: number
  /** at most 200 characters of the matching line; untrusted, render as text only */
  preview: string
}

export interface CodeSearchSummary {
  files_scanned: number
  /** includes content that is not valid UTF-8 */
  binary: number
  too_large: number
  unavailable: number
  matches: number
}

export interface CodeSearchResult {
  matches: CodeSearchMatch[]
  summary: CodeSearchSummary
  truncated: boolean
}

export type OutlineStatus =
  | 'parsed'
  | 'syntax_error'
  | 'too_large'
  | 'binary'
  | 'unsupported_language'

/** One class, function or method of a Python file; the name is untrusted text. */
export interface OutlineSymbol {
  kind: 'class' | 'function' | 'method'
  async: boolean
  name: string
  /** 1-based line of the class/def statement */
  line: number
  end_line: number
  /** 0 = top level */
  depth: number
}

export interface CodeOutline {
  path: string
  status: OutlineStatus
  /** empty unless status is 'parsed' */
  symbols: OutlineSymbol[]
  truncated: boolean
}

/** One import statement of a Python file, as resolved by the server (authoritative). */
export interface ImportEntry {
  line: number
  statement: 'import' | 'from'
  /** "from" module as written; null for "import" and "from . import" */
  module: string | null
  /** leading dots of a relative import; 0 = absolute */
  level: number
  names: string[]
  /** matching repository paths, sorted by the server (at most 5) */
  targets: string[]
  resolution: 'resolved' | 'ambiguous' | 'not_found'
}

export interface CodeImports {
  path: string
  status: OutlineStatus
  /** empty unless status is 'parsed' */
  imports: ImportEntry[]
  truncated: boolean
}

/** One statement of an importer that resolves to the target (Step 39 fields, no targets). */
export type ImporterImport = Omit<ImportEntry, 'targets'>

/** A Python file whose imports resolve to the target file (server-sorted by path). */
export interface Importer {
  path: string
  /** in source order, at most 20 */
  imports: ImporterImport[]
}

export interface ImportersSummary {
  files_scanned: number
  too_large: number
  binary: number
  syntax_error: number
}

export interface CodeImporters {
  path: string
  status: 'scanned' | 'unsupported_language'
  /** empty unless status is 'scanned' */
  importers: Importer[]
  summary: ImportersSummary
  truncated: boolean
}

/** One Python definition (Step 37 symbol fields) and its file. */
export type SymbolMatch = OutlineSymbol & { path: string }

export interface SymbolSearchSummary {
  files_scanned: number
  too_large: number
  binary: number
  syntax_error: number
}

export interface SymbolSearchResult {
  /** server order: by path, then source order */
  results: SymbolMatch[]
  summary: SymbolSearchSummary
  truncated: boolean
}

export interface LanguageStatistic {
  language: string | null
  file_count: number
  total_bytes: number
  percentage: number
}

export interface RepositoryLanguages {
  repository_id: string
  status: RepositoryStatus
  total_files: number
  total_bytes: number
  languages: LanguageStatistic[]
}

export interface Health {
  status: string
}

/** Step 45 go-to-definition outcome for one position of a Python file. */
export type DefinitionStatus =
  | 'resolved'
  | 'ambiguous'
  | 'not_found'
  | 'unsupported'
  | 'no_identifier'
  | 'syntax_error'
  | 'too_large'
  | 'binary'
  | 'unsupported_language'

/** The identifier found at the requested position. */
export interface DefinitionReference {
  path: string
  /** 1-based */
  line: number
  /** 1-based code-point column of the identifier's first character */
  column: number
  name: string
}

/** One definition the identifier resolves to. */
export interface DefinitionLocation {
  path: string
  kind: 'class' | 'function' | 'method' | 'module'
  name: string
  /** 1-based line of the class/def statement (1 for modules) */
  line: number
  end_line: number
}

export interface CodeDefinitionResult {
  status: DefinitionStatus
  /** null unless an identifier was found */
  reference: DefinitionReference | null
  /** server order (by path, then line); empty unless resolved or ambiguous */
  definitions: DefinitionLocation[]
  truncated: boolean
}

/** Step 47 find-references outcome; every status except 'scanned' describes the target. */
export type ReferencesStatus =
  | 'scanned'
  | 'not_a_definition'
  | 'unsupported_target'
  | 'syntax_error'
  | 'too_large'
  | 'binary'
  | 'unsupported_language'

/** One location that statically refers to the target definition. */
export interface ReferenceLocation {
  path: string
  /** 1-based */
  line: number
  /** 1-based code-point column of the identifier's first character */
  column: number
  name: string
  context: 'name' | 'attribute' | 'import'
  /** go-to-definition here also returns other definitions */
  ambiguous: boolean
}

export interface ReferencesSummary {
  files_scanned: number
  too_large: number
  binary: number
  syntax_error: number
  /** counted before the server's reference cap */
  references: number
  /** counted before the server's reference cap */
  ambiguous: number
  unsupported: number
}

export interface CodeReferencesResult {
  status: ReferencesStatus
  /** null unless scanned or unsupported_target */
  target: DefinitionLocation | null
  /** server order (path, line, column); empty unless scanned */
  references: ReferenceLocation[]
  /** all zero unless scanned */
  summary: ReferencesSummary
  truncated: boolean
}

/** Step 49 call-hierarchy outcome; every status except 'scanned' describes the target file. */
export type CallHierarchyStatus =
  | 'scanned'
  | 'not_a_definition'
  | 'syntax_error'
  | 'too_large'
  | 'binary'
  | 'unsupported_language'

/** 'unsupported_target': a method or nested class, whose callers are not searched. */
export type CallersStatus = 'scanned' | 'unsupported_target'

/**
 * One call. It has no path of its own: a caller group's calls are positions in caller.path,
 * a callee group's calls are positions in the response target's file (target.path).
 */
export interface CallSite {
  /** 1-based */
  line: number
  /** 1-based code-point column of the called name */
  column: number
  name: string
  /** go-to-definition here also returns other definitions */
  ambiguous: boolean
}

export interface CallerGroup {
  /** the def, class body or module making the calls */
  caller: DefinitionLocation
  /** before the response caps */
  total_calls: number
  calls: CallSite[]
}

export interface CalleeGroup {
  /** a called class, function or method */
  callee: DefinitionLocation
  /** before the response caps */
  total_calls: number
  calls: CallSite[]
}

export interface CallHierarchySummary {
  files_scanned: number
  too_large: number
  binary: number
  syntax_error: number
  caller_calls: number
  caller_ambiguous: number
  caller_unsupported: number
  non_call_references: number
  callee_calls: number
  callee_ambiguous: number
  callee_unsupported: number
  callee_builtin: number
  callee_unresolved: number
  callee_not_callable: number
  callee_dynamic: number
}

export interface CallHierarchyResult {
  status: CallHierarchyStatus
  /** null unless scanned */
  target: DefinitionLocation | null
  /** null unless scanned */
  callers_status: CallersStatus | null
  /** server order; null unless callers were searched */
  callers: CallerGroup[] | null
  /** server order; empty unless scanned */
  callees: CalleeGroup[]
  summary: CallHierarchySummary
  /** null exactly when callers is null */
  callers_truncated: boolean | null
  callees_truncated: boolean
}

/** Step 51 type-hierarchy outcome; every status except 'scanned' describes the target file. */
export type TypeHierarchyStatus =
  | 'scanned'
  | 'not_a_definition'
  | 'not_a_class'
  | 'syntax_error'
  | 'too_large'
  | 'binary'
  | 'unsupported_language'

/** 'unsupported_target': a class nested in a class, whose subclasses are not searched. */
export type SubclassesStatus = 'scanned' | 'unsupported_target'

export type BaseKind =
  | 'resolved'
  | 'ambiguous'
  | 'not_a_class'
  | 'builtin'
  | 'unsupported'
  | 'unresolved'
  | 'dynamic'

/** A base expression's position. It has no path of its own: it is in the target's file. */
export interface BaseSite {
  /** 1-based */
  line: number
  /** 1-based code-point column of the base name */
  column: number
  name: string
}

export interface BaseEntry {
  /** null exactly when the kind is dynamic */
  site: BaseSite | null
  kind: BaseKind
  /** server order; empty unless resolved, ambiguous or not_a_class */
  definitions: DefinitionLocation[]
}

/** Where a subclass names the target. It has no path of its own: it is in subclass.path. */
export interface SubclassSite {
  /** 1-based */
  line: number
  /** 1-based code-point column of the base name */
  column: number
  name: string
  /** go-to-definition here also returns other definitions */
  ambiguous: boolean
}

export interface SubclassEntry {
  subclass: DefinitionLocation
  site: SubclassSite
}

export interface TypeHierarchySummary {
  files_scanned: number
  too_large: number
  binary: number
  syntax_error: number
  bases: number
  bases_resolved: number
  bases_ambiguous: number
  bases_builtin: number
  bases_unresolved: number
  bases_unsupported: number
  bases_dynamic: number
  bases_not_a_class: number
  keyword_arguments: number
  subclasses: number
  subclasses_ambiguous: number
  subclass_unsupported: number
  non_base_references: number
}

export interface TypeHierarchyResult {
  status: TypeHierarchyStatus
  /** null unless scanned */
  target: DefinitionLocation | null
  /** server order, duplicates kept; empty unless scanned */
  bases: BaseEntry[]
  /** null unless scanned */
  subclasses_status: SubclassesStatus | null
  /** server order; null unless subclasses were searched */
  subclasses: SubclassEntry[] | null
  summary: TypeHierarchySummary
  bases_truncated: boolean
  /** null exactly when subclasses is null */
  subclasses_truncated: boolean | null
}

/** Step 53 method-overrides outcome; every status except 'scanned' describes the target file. */
export type MethodOverridesStatus =
  | 'scanned'
  | 'not_a_definition'
  | 'not_a_method'
  | 'syntax_error'
  | 'too_large'
  | 'binary'
  | 'unsupported_language'

/** 'unsupported_target': the method's class is nested in a class; its subclasses are not searched. */
export type OverridingStatus = 'scanned' | 'unsupported_target'

/** Whether a related class's own body defines the method (decorators are never interpreted). */
export type MemberStatus = 'defined' | 'not_defined' | 'not_a_method' | 'unsupported' | 'unavailable'

/** One direct base's class candidate, judged for the method. */
export interface OverriddenEntry {
  /** a position in the response target's file; null for a dynamic base */
  site: BaseSite | null
  base_kind: BaseKind
  /** one class candidate of the base (the JSON key is "class"); null exactly when unavailable */
  class: DefinitionLocation | null
  /** the base resolves to several definitions */
  ambiguous: boolean
  status: MemberStatus
  /** the class's same-name methods, server order (cut at 20); empty unless defined */
  implementations: DefinitionLocation[]
  /** the class defines the name more than once (counted before the cut) */
  implementations_ambiguous: boolean
}

/** One direct subclass entry whose own body binds the method's name. */
export interface OverridingEntry {
  subclass: DefinitionLocation
  /** a position in subclass.path */
  site: SubclassSite
  status: 'defined' | 'not_a_method' | 'unsupported'
  implementations: DefinitionLocation[]
  implementations_ambiguous: boolean
}

export interface MethodOverridesSummary {
  files_scanned: number
  too_large: number
  binary: number
  syntax_error: number
  bases: number
  base_classes: number
  overridden_defined: number
  overridden_not_defined: number
  overridden_not_a_method: number
  overridden_unavailable: number
  overridden_unsupported: number
  subclasses: number
  overriding: number
  /** subclass entries not binding the name: counted, never listed */
  inheriting: number
  overriding_not_a_method: number
  overriding_unsupported: number
}

export interface MethodOverridesResult {
  status: MethodOverridesStatus
  /** the method; null unless scanned */
  target: DefinitionLocation | null
  /** the method's class; null unless scanned */
  owner: DefinitionLocation | null
  /** server order (base order, then candidate order); empty unless scanned */
  overridden: OverriddenEntry[]
  /** null unless scanned */
  overriding_status: OverridingStatus | null
  /** server order with duplicates; null unless subclasses were searched */
  overriding: OverridingEntry[] | null
  summary: MethodOverridesSummary
  overridden_truncated: boolean
  /** null exactly when overriding is null */
  overriding_truncated: boolean | null
}

/**
 * Step 55: one identifier load of a Python file with no statically known binding, as found by
 * the server. Not a runtime error: names defined dynamically (exec, globals()) are listed,
 * names that may come from a star import are only counted.
 */
export interface UnboundName {
  /** 1-based */
  line: number
  /** 1-based code-point column of the identifier (shown, never used for navigation) */
  column: number
  /** the identifier as Python sees it (NFKC); untrusted text */
  name: string
}

/** The server's counts, taken before its 500-result cap (authoritative, never recounted). */
export interface UnboundNamesSummary {
  unbound: number
  distinct: number
  /** would-be-unbound loads in a module with a star import (never listed) */
  unsupported: number
  /** loads of interpreter-provided names such as __file__ (never listed) */
  implicit: number
}

export interface UnboundNamesResult {
  path: string
  status: OutlineStatus
  /** source order, every occurrence; empty unless status is 'parsed' */
  names: UnboundName[]
  summary: UnboundNamesSummary
  truncated: boolean
}

/** Step 58 name-occurrences outcome for one position of a Python file. */
export type OccurrencesStatus =
  | 'found'
  | 'not_bound'
  | 'unsupported'
  | 'no_identifier'
  | 'syntax_error'
  | 'too_large'
  | 'binary'
  | 'unsupported_language'

export type OccurrenceKind = 'read' | 'write' | 'delete' | 'parameter' | 'definition'

/** The scope that holds the binding (Step 45's scope kinds). */
export type OccurrenceScope = 'module' | 'function' | 'class' | 'lambda' | 'comprehension'

/** One occurrence of the binding, in the reference's file. */
export interface Occurrence {
  /** 1-based */
  line: number
  /** 1-based code-point column of the identifier's first character */
  column: number
  kind: OccurrenceKind
}

/** The server's counts, taken before its 1,000-occurrence cap (authoritative, never recounted). */
export interface OccurrencesSummary {
  occurrences: number
  reads: number
  writes: number
  deletes: number
  /** parameter and definition sites */
  bindings: number
}

export interface CodeOccurrencesResult {
  status: OccurrencesStatus
  /** null unless an identifier was found */
  reference: DefinitionReference | null
  /** null unless found */
  scope: OccurrenceScope | null
  /** source order, at most 1,000; empty unless found */
  occurrences: Occurrence[]
  summary: OccurrencesSummary
  truncated: boolean
}

/**
 * Step 60: one assignment to a def/async def local variable that nothing in the file reads, as
 * found by the server (variable-level "never read anywhere", not a data-flow analysis).
 */
export interface UnusedVariable {
  /** 1-based */
  line: number
  /** 1-based code-point column of the assigned name (shown, never used for navigation) */
  column: number
  /** the identifier as Python sees it (NFKC); untrusted text */
  name: string
}

/** The server's counts, taken before its 500-entry cap (authoritative, never recounted). */
export interface UnusedVariablesSummary {
  /** listed assignments */
  unused: number
  /** distinct listed variables: (function scope, name) */
  distinct: number
  /** never-read variables of dynamic functions or named in string annotations (never listed) */
  unsupported: number
}

export interface UnusedVariablesResult {
  path: string
  status: OutlineStatus
  /** source order, every assignment, at most 500; empty unless status is 'parsed' */
  variables: UnusedVariable[]
  summary: UnusedVariablesSummary
  truncated: boolean
}

/**
 * Step 62: one import whose bound name nothing in the file reads, as found by the server (an
 * import kept only for its side effects is listed too).
 */
export interface UnusedImport {
  /** 1-based */
  line: number
  /** 1-based code-point column of the bound name (shown, never used for navigation) */
  column: number
  /** the bound name as Python sees it (NFKC); untrusted text */
  name: string
}

/** The server's counts, taken before its 500-entry cap (authoritative, never recounted). */
export interface UnusedImportsSummary {
  /** listed imports */
  unused: number
  /** distinct listed bindings: (scope, name) */
  distinct: number
  /** never-read imports of package/stub interfaces, dynamic code or string annotations (never listed) */
  unsupported: number
}

export interface UnusedImportsResult {
  path: string
  status: OutlineStatus
  /** source order, every import, at most 500; empty unless status is 'parsed' */
  imports: UnusedImport[]
  summary: UnusedImportsSummary
  truncated: boolean
}
