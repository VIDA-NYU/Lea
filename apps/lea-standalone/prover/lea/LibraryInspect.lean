import Lean

/- Compiler inspection only. Included in temporary files by the host; no project declarations
   or imports are changed. Fingerprints erase binder names, not hypotheses or constants. -/
open Lean Meta Elab Command

namespace LeaLibraryInspector

private def levelJson (params : List Name) : Level → Json
  | .zero => toJson ("zero" : String)
  | .succ l => toJson #[toJson ("succ" : String), levelJson params l]
  | .max a b => toJson #[toJson ("max" : String), levelJson params a, levelJson params b]
  | .imax a b => toJson #[toJson ("imax" : String), levelJson params a, levelJson params b]
  | .param n => toJson #[toJson ("param" : String), toJson (params.idxOf n)]
  | .mvar n => toJson n.name.toString

private def exprJson (params : List Name) : Expr → Json
  | .bvar n => toJson #[toJson ("bound" : String), toJson n]
  | .fvar n => toJson #[toJson ("free" : String), toJson n.name.toString]
  | .mvar n => toJson #[toJson ("meta" : String), toJson n.name.toString]
  | .sort l => toJson #[toJson ("sort" : String), levelJson params l]
  | .const n ls => toJson #[toJson ("constant" : String), toJson n.toString,
      toJson (ls.map (levelJson params))]
  | .app f a => toJson #[toJson ("apply" : String), exprJson params f, exprJson params a]
  | .lam _ t b bi => toJson #[toJson ("lambda" : String), toJson (reprStr bi),
      exprJson params t, exprJson params b]
  | .forallE _ t b bi => toJson #[toJson ("forall" : String), toJson (reprStr bi),
      exprJson params t, exprJson params b]
  | .letE _ t v b _ => toJson #[toJson ("let" : String), exprJson params t,
      exprJson params v, exprJson params b]
  | .lit (.natVal n) => toJson #[toJson ("nat" : String), toJson n]
  | .lit (.strVal s) => toJson #[toJson ("string" : String), toJson s]
  | .mdata _ e => exprJson params e
  | .proj n i e => toJson #[toJson ("projection" : String), toJson n.toString,
      toJson i, exprJson params e]

private def inspect (ci : ConstantInfo) : MetaM Json := do
  let kind := match ci with
    | .thmInfo _ => "theorem"
    | .axiomInfo _ => "axiom"
    | .inductInfo _ => "inductive"
    | .ctorInfo _ => "constructor"
    | .recInfo _ => "recursor"
    | .opaqueInfo _ => "opaque"
    | _ => "definition"
  let body ← match ci with
    | .defnInfo d => pure (exprJson ci.levelParams d.value)
    | .opaqueInfo d => pure (exprJson ci.levelParams d.value)
    | .inductInfo d => do
      let mut constructors : Array Json := #[]
      for name in d.ctors do
        let ctor ← getConstInfo name
        constructors := constructors.push (toJson #[toJson name.toString, exprJson ctor.levelParams ctor.type])
      pure (toJson constructors)
    | _ => pure Json.null
  let typ ← withOptions (fun opts => (opts.setBool `pp.fullNames true).setBool `pp.universes true)
    (ppExpr ci.type)
  let axioms ← collectAxioms ci.name
  let ranges ← findDeclarationRanges? ci.name
  let range := match ranges with
    | none => Json.null
    | some r => Json.mkObj [
      ("start_line", toJson r.range.pos.line), ("start_column", toJson r.range.pos.column),
      ("end_line", toJson r.range.endPos.line), ("end_column", toJson r.range.endPos.column)]
  let dependencies := ci.type.getUsedConstants ++ (match ci.value? with
    | none => #[]
    | some value => value.getUsedConstants)
  return Json.mkObj [
    ("full_name", toJson ci.name.toString),
    ("user_name", toJson (privateToUserName ci.name).toString),
    ("visibility", toJson (if isPrivateName ci.name then "private" else "public")),
    ("kind", toJson kind), ("type", toJson typ.pretty),
    ("level_params", toJson (ci.levelParams.map Name.toString)),
    ("type_expr", exprJson ci.levelParams ci.type), ("body_expr", body),
    ("axioms", toJson (axioms.map Name.toString)),
    ("dependencies", toJson (dependencies.map Name.toString)), ("range", range)]

end LeaLibraryInspector

elab "#lea_inspect_module " moduleName:str : command => do
  let env ← getEnv
  let moduleIdx := (env.header.moduleNames.find? fun name =>
    name.toString == moduleName.getString).bind fun name => env.getModuleIdx? name
  if moduleIdx.isNone then
    throwError "Requested compiled module {moduleName.getString} is not loaded"
  let mut results : Array Json := #[]
  for (name, ci) in env.constants.toList do
    if moduleIdx.isSome && env.getModuleIdxFor? name == moduleIdx then
      results := results.push (← liftTermElabM <| LeaLibraryInspector.inspect ci)
  logInfo m!"LEA_LIBRARY_JSON:{(toJson results).compress}"

elab "#lea_inspect_decl " declName:str : command => do
  let env ← getEnv
  match env.find? declName.getString.toName with
  | none => throwError "Unknown declaration {declName.getString}"
  | some ci =>
    let result ← liftTermElabM <| LeaLibraryInspector.inspect ci
    logInfo m!"LEA_LIBRARY_JSON:{result.compress}"

/- Source-local inspection also works for private names. The line cutoff excludes
   the inspector itself when its commands are appended to a submitted source. -/
elab "#lea_inspect_source " lastLine:num : command => do
  let env ← getEnv
  let mut results : Array Json := #[]
  for (name, ci) in env.constants.toList do
    if (env.getModuleIdxFor? name).isNone then
      if let some ranges ← findDeclarationRanges? name then
        if ranges.range.pos.line ≤ lastLine.getNat then
          results := results.push (← liftTermElabM <| LeaLibraryInspector.inspect ci)
  logInfo m!"LEA_LIBRARY_JSON:{(toJson results).compress}"
