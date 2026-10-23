/-
Adapted from https://github.com/leanprover/lean4checker/blob/master/Main.lean
and
https://github.com/kim-em/lean-training-data/blob/master/scripts/declaration_types.lean
-/

import SafeVerify
import SafeVerify.Monads
import Lean
import Cli

open Lean Meta Core SafeVerify
open Std

/-- Takes the environment obtained after replaying all the constant in a file and outputs
a hashmap storing the infos corresponding to all the theorems and definitions in the file. -/
def processFileDeclarations (env : Environment) : HashMap Name Info := Id.run do
  let mut out : HashMap Name Info := {}
  for (_, ci) in env.constants.map₂  do
    if ci.kind ∈ ["theorem", "def", "opaque", "inductive", "constructor"] then
      let (_, s) := (CollectAxioms.collect ci.name).run env |>.run {}
      out := out.insert ci.name ⟨ci, s.axioms⟩
  return out

/-- Lean generates auxiliary `_unsafe_rec` runtime shims for ordinary accepted
recursive definitions. These are compiler artifacts, not evidence that the user
wrote `partial`. -/
def isCompilerUnsafeRecName : Name → Bool
  | .str _ s => s == "_unsafe_rec"
  | _ => false

/-- Check if an Info uses only allowed axioms -/
def checkAxioms (info : Info) (allowedAxioms : Array Name) : Bool := Id.run do
  for a in info.axioms do
    if a ∉ allowedAxioms then return false
  return true

def Info.toFailureMode (target submission : Info) (allowedAxioms : Array Name) : Option SafeVerifyOutcome := Id.run do
  if target.constInfo.kind ≠ submission.constInfo.kind then
    return some ⟨target, submission, some <| .kind target.constInfo.kind submission.constInfo.kind⟩
  if submission.constInfo.kind == "theorem" && !equivThm target.constInfo submission.constInfo then
    return some ⟨target, submission, some .thmType⟩
  if submission.constInfo.kind == "def"
      && !equivDefn target.constInfo submission.constInfo (`sorryAx ∉ target.axioms)  then
    return some ⟨target, submission, some .defnCheck⟩
  if submission.constInfo.kind == "opaque" && !equivOpaq target.constInfo submission.constInfo then
    return some ⟨target, submission, some .opaqueCheck⟩
  if submission.constInfo.kind == "constructor" && !equivCtor target.constInfo submission.constInfo then
    return some ⟨target, submission, some .ctorCheck⟩
  if !checkAxioms submission allowedAxioms then
    return some ⟨target, submission, some .axioms⟩
  return some ⟨target, submission, none⟩

/-- Check that the declarations match. Reads Settings and Decls from context, updates State with outcomes. -/
def checkTargets : SafeVerifyM Unit := do
  let settings ← getSettings
  let decls ← getDecls
  let lookupTarget := fun n => decls.targetDecls.get? n |>.map (·.constInfo)
  let lookupNew := fun n => decls.submissionDecls.get? n |>.map (·.constInfo)
  let checkOutcome := decls.targetDecls.map fun _ targetInfo ↦ Id.run do
    let optionInfo := decls.submissionDecls.get? targetInfo.constInfo.name
    -- For inductives, check via equivInduct which needs the full hashmaps for constructor lookup
    if targetInfo.constInfo.kind == "inductive" then
      if let some submissionInfo := optionInfo then
        if targetInfo.constInfo.kind ≠ submissionInfo.constInfo.kind then
          return ⟨targetInfo, some submissionInfo, some <| .kind targetInfo.constInfo.kind submissionInfo.constInfo.kind⟩
        if !equivInduct targetInfo.constInfo submissionInfo.constInfo lookupTarget lookupNew then
          return ⟨targetInfo, some submissionInfo, some .inductCheck⟩
        if !checkAxioms submissionInfo settings.allowedAxioms then
          return ⟨targetInfo, some submissionInfo, some .axioms⟩
        return ⟨targetInfo, some submissionInfo, none⟩
      else
        return ⟨targetInfo, none, some .notFound⟩
    let optionOutcome := optionInfo.bind (Info.toFailureMode targetInfo · settings.allowedAxioms)
    optionOutcome.getD (dflt := ⟨targetInfo, none, some .notFound⟩)
  modify fun s => { s with checkOutcomes := checkOutcome }

/-- Deep-copy a universe level, rebuilding every node from scratch.
This breaks references to corrupted Level objects (e.g., via unsafeCast). -/
partial def rebuildLevel : Level → Level
  | .zero => .zero
  | .succ l => .succ (rebuildLevel l)
  | .max l1 l2 => .max (rebuildLevel l1) (rebuildLevel l2)
  | .imax l1 l2 => .imax (rebuildLevel l1) (rebuildLevel l2)
  | .param n => .param n
  | .mvar id => .mvar id

/-- Deep-copy an expression, rebuilding every node from scratch.
This breaks references to compacted regions whose runtime representation
may have been corrupted (e.g., via unsafeCast at elaboration time). -/
partial def rebuildExpr : Expr → Expr
  | .bvar i => .bvar i
  | .fvar id => .fvar id
  | .mvar id => .mvar id
  | .sort l => .sort (rebuildLevel l)
  | .const n ls => .const n (ls.map rebuildLevel)
  | .lit (.natVal n) => .lit (.natVal n)
  | .lit (.strVal s) => .lit (.strVal s)
  | .app f a => .app (rebuildExpr f) (rebuildExpr a)
  | .lam n t b bi => .lam n (rebuildExpr t) (rebuildExpr b) bi
  | .forallE n t b bi => .forallE n (rebuildExpr t) (rebuildExpr b) bi
  | .letE n t v b nd => .letE n (rebuildExpr t) (rebuildExpr v) (rebuildExpr b) nd
  | .mdata m e => .mdata m (rebuildExpr e)
  | .proj s i e => .proj s i (rebuildExpr e)

/-- Sanitize a ConstantInfo by rebuilding all expressions from scratch. -/
def sanitizeConstant : ConstantInfo → ConstantInfo
  | .defnInfo d => .defnInfo { d with
      type := rebuildExpr d.type
      value := rebuildExpr d.value }
  | .thmInfo t => .thmInfo { t with
      type := rebuildExpr t.type
      value := rebuildExpr t.value }
  | .opaqueInfo o => .opaqueInfo { o with
      type := rebuildExpr o.type
      value := rebuildExpr o.value }
  | ci => ci

/-- Replays a lean file and outputs a hashmap storing the `Info`s corresponding to
the theorems and definitions in the file. -/
def replayFile (filePath : System.FilePath) (disallowPartial : Bool) : IO (HashMap Name Info) := do
  IO.eprintln s!"Replaying {filePath}"
  unless (← filePath.pathExists) do
    throw <| IO.userError s!"object file '{filePath}' does not exist"
  let (mod, _) ← readModuleData filePath
  let env ← importModules mod.imports {} 0
  IO.eprintln "Finished setting up the environment."
  let mut newConstants := {}
  for name in mod.constNames, ci in mod.constants do
    if ci.isUnsafe then
      throw <| IO.userError s!"unsafe constant {name} detected"
    if disallowPartial && ci.isPartial && !isCompilerUnsafeRecName name then
      throw <| IO.userError s!"partial constant {name} detected"
    newConstants := newConstants.insert name (sanitizeConstant ci)
  let env ← env.replay newConstants
  IO.eprintln s!"Finished replay. Found {newConstants.size} declarations."
  -- Verify theorem proofs using kernel typechecker with rebuilt expressions.
  for name in mod.constNames, ci in mod.constants do
    if let .thmInfo t := ci then
      let freshValue := rebuildExpr t.value
      let freshType := rebuildExpr t.type
      match Kernel.check env {} freshValue with
      | .ok inferredType =>
        match Kernel.isDefEq env {} inferredType freshType with
        | .ok true => pure ()
        | _ => throw <| IO.userError s!"kernel verification failed for '{name}': inferred type does not match declared type"
      | .error _ =>
        throw <| IO.userError s!"kernel verification failed for '{name}': proof term rejected by kernel typechecker (possible unsafeCast or compacted-region corruption)"
  return processFileDeclarations env

/-- Replays the target (challenge) file and extracts declarations. -/
def replayChallenges : ReaderT SafeVerify.Settings IO (HashMap Name Info) := do
  let settings ← read
  replayFile settings.targetFile settings.disallowPartial

/-- Replays the submission (solution) file and extracts declarations. -/
def replaySolutions : ReaderT SafeVerify.Settings IO (HashMap Name Info) := do
  let settings ← read
  replayFile settings.submissionFile settings.disallowPartial

/-- Replay a file and return both the new-declaration HashMap AND the full Environment.
Used for the submission so we can look up imported declarations as a fallback. -/
def replayFileWithEnv (filePath : System.FilePath) (disallowPartial : Bool)
    : IO (HashMap Name Info × Environment) := do
  IO.eprintln s!"Replaying {filePath}"
  unless (← filePath.pathExists) do
    throw <| IO.userError s!"object file '{filePath}' does not exist"
  let (mod, _) ← readModuleData filePath
  let env ← importModules mod.imports {} 0
  IO.eprintln "Finished setting up the environment."
  let mut newConstants := {}
  for name in mod.constNames, ci in mod.constants do
    if ci.isUnsafe then
      throw <| IO.userError s!"unsafe constant {name} detected"
    if disallowPartial && ci.isPartial && !isCompilerUnsafeRecName name then
      throw <| IO.userError s!"partial constant {name} detected"
    newConstants := newConstants.insert name (sanitizeConstant ci)
  let env ← env.replay newConstants
  IO.eprintln s!"Finished replay. Found {newConstants.size} declarations."
  for name in mod.constNames, ci in mod.constants do
    if let .thmInfo t := ci then
      let freshValue := rebuildExpr t.value
      let freshType := rebuildExpr t.type
      match Kernel.check env {} freshValue with
      | .ok inferredType =>
        match Kernel.isDefEq env {} inferredType freshType with
        | .ok true => pure ()
        | _ => throw <| IO.userError s!"kernel verification failed for '{name}': inferred type does not match declared type"
      | .error _ =>
        throw <| IO.userError s!"kernel verification failed for '{name}': proof term rejected by kernel typechecker (possible unsafeCast or compacted-region corruption)"
  return (processFileDeclarations env, env)

/-- Read module imports from an olean file without full replay. -/
def readImports (filePath : System.FilePath) : IO (Array Import) := do
  unless (← filePath.pathExists) do
    throw <| IO.userError s!"object file '{filePath}' does not exist"
  let (mod, _) ← readModuleData filePath
  return mod.imports

/-- Check that submission's transitive imports cover all of target's transitive imports.
This prevents attacks where submissions omit imports to redefine types.
Both sides are resolved to their full transitive module sets (from Environment.header),
so multi-file repos that transitively include Mathlib pass correctly, and barrel imports
like `import Mathlib` are expanded to the individual modules they bring in. -/
def checkImportSuperset (targetFile submissionFile : System.FilePath)
    (targetImports submissionImports : Array Import) : IO Unit := do
  -- Both target and submission must import Init
  unless targetImports.any (·.module == `Init) do
    throw <| IO.userError s!"Target '{targetFile}' does not import Init. Refusing to verify against a prelude-based target."
  if submissionImports.isEmpty then
    throw <| IO.userError s!"'{submissionFile}' has no imports (possible prelude file). Submissions must import Init to prevent kernel type redefinition."
  -- Build both environments to get transitive module sets
  let targetEnv ← importModules targetImports {} 0
  let submissionEnv ← importModules submissionImports {} 0
  let submissionModuleSet : Std.HashSet Name :=
    submissionEnv.header.moduleNames.foldl (init := {}) fun s n => s.insert n
  let mut missing : Array Name := #[]
  for mod in targetEnv.header.moduleNames do
    unless submissionModuleSet.contains mod do
      missing := missing.push mod
  unless missing.isEmpty do
    -- Report only first few to avoid overwhelming output
    let shown := if missing.size > 10 then
      s!"{missing[:10]} ... and {missing.size - 10} more"
    else
      s!"{missing}"
    throw <| IO.userError s!"Submission '{submissionFile}' is missing {missing.size} transitive imports required by target: {shown}. Submissions must transitively import at least everything the target imports to prevent type redefinition attacks."

/-- Recursively find all Nat literals in an expression. -/
partial def collectNatLiterals : Expr → Array (Nat × String)
  | .lit (.natVal n) =>
    let shown := toString (Expr.lit (.natVal n))
    #[(n, shown)]
  | .app f a => collectNatLiterals f ++ collectNatLiterals a
  | .lam _ t b _ => collectNatLiterals t ++ collectNatLiterals b
  | .forallE _ t b _ => collectNatLiterals t ++ collectNatLiterals b
  | .letE _ t v b _ => collectNatLiterals t ++ collectNatLiterals v ++ collectNatLiterals b
  | .mdata _ e => collectNatLiterals e
  | .proj _ _ e => collectNatLiterals e
  | _ => #[]

/-- Validate Nat literals in newly introduced declarations.
Reject suspicious Nat literals that print as negative (unsafeCast corruption). -/
def validateNewDefinitionNatLiterals
    (targetInfos submissionInfos : HashMap Name Info) : IO Unit := do
  for (name, info) in submissionInfos do
    if targetInfos.get? name |>.isNone then
      let exprs := match info.constInfo with
        | .defnInfo d => #[d.type, d.value]
        | .thmInfo t => #[t.type, t.value]
        | .opaqueInfo o => #[o.type, o.value]
        | .inductInfo i => #[i.type]
        | .ctorInfo c => #[c.type]
        | .recInfo r => #[r.type]
        | _ => #[]
      for e in exprs do
        for (n, shown) in collectNatLiterals e do
          if shown.startsWith "-" then
            throw <| IO.userError s!"suspicious Nat literal in new declaration '{name}': stored natVal={n} but renders as '{shown}' (possible unsafeCast corruption)"

/-- Print verbose information about a type mismatch between two constants. -/
def printVerboseTypeMismatch (targetConst submissionConst : ConstantInfo) : IO Unit := do
  IO.eprintln s!"  Expected type: {targetConst.type}"
  IO.eprintln s!"  Got type:      {submissionConst.type}"
  if targetConst.levelParams != submissionConst.levelParams then
    IO.eprintln s!"  Expected level params: {targetConst.levelParams}"
    IO.eprintln s!"  Got level params:      {submissionConst.levelParams}"

/-- Print verbose information about a definition mismatch. -/
def printVerboseDefnMismatch (targetConst submissionConst : ConstantInfo) : IO Unit := do
  if targetConst.type != submissionConst.type then
    IO.eprintln s!"  Type mismatch:"
    IO.eprintln s!"    Expected: {targetConst.type}"
    IO.eprintln s!"    Got:      {submissionConst.type}"
  if targetConst.levelParams != submissionConst.levelParams then
    IO.eprintln s!"  Level params mismatch:"
    IO.eprintln s!"    Expected: {targetConst.levelParams}"
    IO.eprintln s!"    Got:      {submissionConst.levelParams}"
  if let (.defnInfo tval₁, .defnInfo tval₂) := (targetConst, submissionConst) then
    if tval₁.safety != tval₂.safety then
      IO.eprintln s!"  Safety mismatch: expected {tval₁.safety}, got {tval₂.safety}"
    if tval₁.value != tval₂.value then
      IO.eprintln s!"  Value mismatch (values differ)"

/-- Print verbose information about an opaque mismatch. -/
def printVerboseOpaqueMismatch (targetConst submissionConst : ConstantInfo) : IO Unit := do
  if targetConst.type != submissionConst.type then
    IO.eprintln s!"  Type mismatch:"
    IO.eprintln s!"    Expected: {targetConst.type}"
    IO.eprintln s!"    Got:      {submissionConst.type}"
  if targetConst.levelParams != submissionConst.levelParams then
    IO.eprintln s!"  Level params mismatch:"
    IO.eprintln s!"    Expected: {targetConst.levelParams}"
    IO.eprintln s!"    Got:      {submissionConst.levelParams}"
  if let (.opaqueInfo tval₁, .opaqueInfo tval₂) := (targetConst, submissionConst) then
    if tval₁.isUnsafe != tval₂.isUnsafe then
      -- TODO(Paul-Lez): currently this will never occur because we throw an error whenever we reach an unsafe constant - fix this?
      -- probably we should track disallowed opaque (and partial) constant in a CheckFailureField.
      IO.eprintln s!"  Safety mismatch: expected isUnsafe={tval₁.isUnsafe}, got isUnsafe={tval₂.isUnsafe}"
    if tval₁.value != tval₂.value then
      IO.eprintln s!"  Value mismatch (values differ)"

/-- Run the main SafeVerify check. Uses the three-layer monadic design with Settings, Decls, and State. -/
def runSafeVerify : SafeVerifyM Unit := do
  let settings ← getSettings
  let decls ← getDecls

  IO.eprintln "------------------"
  -- Check for disallowed axioms and record violations in state
  for (n, info) in decls.submissionDecls do
    if !checkAxioms info settings.allowedAxioms then
      let disallowed := info.axioms.filter (· ∉ settings.allowedAxioms)
      IO.eprintln s!"{n} used disallowed axioms. {info.axioms}"
      modify fun s => { s with axiomViolations := s.axiomViolations.push (n, disallowed) }

  -- Run the declaration checks and store outcomes in state
  checkTargets
  IO.eprintln "------------------"

  -- Print results
  let state ← get
  let checkOutcome := state.checkOutcomes
  for (name, outcome) in checkOutcome do
    if let some failure := outcome.failureMode then
      IO.eprintln s!"Found a problem in {settings.submissionFile} with declaration {name}: {failure}"
      if settings.verbose then
        match failure with
        | .thmType =>
          if let some submissionConst := outcome.solutionInfo then
            printVerboseTypeMismatch outcome.targetInfo.constInfo submissionConst.constInfo
        | .defnCheck =>
          if let some submissionConst := outcome.solutionInfo then
            printVerboseDefnMismatch outcome.targetInfo.constInfo submissionConst.constInfo
        | .opaqueCheck =>
          if let some submissionConst := outcome.solutionInfo then
            printVerboseOpaqueMismatch outcome.targetInfo.constInfo submissionConst.constInfo
        | .axioms =>
          if let some info := decls.submissionDecls.get? name then
            IO.eprintln s!"  Disallowed axioms used: {info.axioms.filter (· ∉ settings.allowedAxioms)}"
        | _ => pure ()
  IO.eprintln "------------------"

open Cli

instance : ParseableType System.FilePath where
  name := "System.FilePath"
  parse? str := some { toString := str }

/-- Convert parsed CLI arguments to SafeVerify Settings -/
def settingsFromParsed (p : Parsed) : SafeVerify.Settings where
  targetFile := p.positionalArg! "target" |>.as! System.FilePath
  submissionFile := p.positionalArg! "submission" |>.as! System.FilePath
  disallowPartial := p.hasFlag "disallow-partial"
  verbose := p.hasFlag "verbose"
  allowedAxioms := #[`propext, `Quot.sound, `Classical.choice]
  jsonOutputPath := p.flag? "save" |>.map (·.as! System.FilePath)
  selectedDeclaration := p.flag? "declaration" |>.map (fun value => value.as! String |>.toName)

/--
Takes two olean files, and checks whether the second file
implements the theorems and definitions specified in the first file.
First file (the target) may contain theorem / function signature with sorry in their bodies;
the second file is expected to fill them.
Uses Environment.replay to defend against manipulation of environment.
Checks the second file's theorems to make sure they only use the three standard axioms.
-/
def runMain (p : Parsed) : IO UInt32 := do
  initSearchPath (← findSysroot)
  IO.eprintln s!"Currently running on Lean v{Lean.versionString}"

  -- Create settings from CLI arguments
  let settings := settingsFromParsed p
  IO.eprintln s!"Running SafeVerify on target file: {settings.targetFile} and submission file: {settings.submissionFile}."

  -- Import superset check: submission must import everything the target does
  let targetImports ← readImports settings.targetFile
  let submissionImports ← readImports settings.submissionFile
  checkImportSuperset settings.targetFile settings.submissionFile targetImports submissionImports

  -- Replay files to get declarations (runs in ReaderT Settings IO)
  IO.eprintln "------------------"
  let targetDecls ← replayChallenges.run settings
  IO.eprintln "------------------"
  let (submissionDecls, submissionEnv) ← replayFileWithEnv settings.submissionFile settings.disallowPartial

  -- Supplement submissionDecls with imported declarations that the target also defines.
  -- This handles the case where the spec replicates definitions that in the impl are in
  -- imported modules (e.g., spec defines Problem6.graphLaplacian, impl imports it from
  -- an auxiliary module). Without this, SafeVerify would report "not found" because it
  -- only sees the impl module's own new constants (map₂), not imported ones (map₁).
  let mut supplementedDecls := submissionDecls
  for (name, _) in targetDecls do
    if submissionDecls.get? name |>.isNone then
      if let some ci := submissionEnv.find? name then
        if ci.kind ∈ ["theorem", "def", "opaque", "inductive", "constructor"] then
          let (_, s) := (CollectAxioms.collect name).run submissionEnv |>.run {}
          supplementedDecls := supplementedDecls.insert name ⟨ci, s.axioms⟩
          IO.eprintln s!"  Note: '{name}' found in submission's imported environment"

  -- Validate Nat literals in new declarations
  validateNewDefinitionNatLiterals targetDecls submissionDecls

  -- Create the Decls context
  let decls : SafeVerify.Decls ← match settings.selectedDeclaration with
    | none => pure { targetDecls := targetDecls, submissionDecls := supplementedDecls }
    | some name => do
      let some info := submissionDecls.get? name
        | throw <| IO.userError s!"selected declaration '{name}' is absent from the compiled module"
      -- Replay above checks the actual module's proof terms. Limit correspondence
      -- and axiom checks to the selected result; its axiom set is transitive.
      -- No source is rewritten and private names keep their actual module identity.
      let selected := ({} : HashMap Name Info).insert name info
      pure { targetDecls := selected, submissionDecls := selected }

  -- Run the main SafeVerify check (runs in ReaderT Settings (ReaderT Decls (StateT State IO)))
  let (_, finalState) ← (runSafeVerify.run settings |>.run decls |>.run {})

  -- Save JSON output if requested (always, even on failure)
  if let some jsonPath := settings.jsonOutputPath then
    let jsonOutput := ToJson.toJson finalState.checkOutcomes.toArray
    IO.FS.writeFile jsonPath (ToString.toString jsonOutput)

  -- Check for failures: both check outcomes and axiom violations
  let hasCheckFailures := finalState.checkOutcomes.any fun _ outcome =>
    outcome.failureMode.isSome
  let hasAxiomViolations := !finalState.axiomViolations.isEmpty
  if hasCheckFailures || hasAxiomViolations then
    let nonVerboseMsg :=
      " For more diagnostic information about failures, run safe_verify with the -v (or --verbose) flag."
    throw <| IO.userError s!"SafeVerify check failed.{if !settings.verbose then nonVerboseMsg else ""}"
  else
    IO.eprintln "SafeVerify check passed."
  return 0

/-- The main CLI interface for `SafeVerify`. This will be expanded as we add more
functionalities.-/
def mainCmd : Cmd := `[Cli|
  mainCmd VIA runMain;
  "Run SafeVerify on a pair of files (TargetFile, SubmissionFile). "
  FLAGS:
    "disallow-partial"; "Disallow partial definitions"
    v, "verbose"; "Enable verbose error messages showing detailed type information"
    s, "save" : System.FilePath; "Save output to a JSON file"
    "declaration" : String; "Audit one actual compiled declaration and its transitive axioms"

  ARGS:
    target : System.FilePath; "The target file"
    submission : System.FilePath; "The submission file"
]

def main (args : List String) : IO UInt32 := do
  mainCmd.validate args
