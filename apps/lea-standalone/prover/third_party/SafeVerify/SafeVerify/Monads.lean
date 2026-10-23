import SafeVerify.Types
import Lean

open Lean

namespace SafeVerify

/-- Configuration settings for the SafeVerify check.
    This contains CLI flags and configuration that don't change. -/
structure Settings where
  /-- Path to the target file containing theorem/definition specifications -/
  targetFile : System.FilePath := default
  /-- Path to the submission file containing implementations -/
  submissionFile : System.FilePath := default
  /-- Whether to disallow partial definitions -/
  disallowPartial : Bool := false
  /-- Whether to enable verbose error output -/
  verbose : Bool := false
  /-- List of axioms that are allowed to be used -/
  allowedAxioms : Array Name := #[`propext, `Quot.sound, `Classical.choice]
  /-- Optional path to save JSON output -/
  jsonOutputPath : Option System.FilePath := none
  /-- Audit this actual compiled result against its own type after kernel replay. -/
  selectedDeclaration : Option Name := none
deriving Inhabited

/-- Parsed declarations from target and submission files.
    This is computed once and then read-only. -/
structure Decls where
  /-- Declarations parsed from the target file -/
  targetDecls : Std.HashMap Name Info := {}
  /-- Declarations parsed from the submission file -/
  submissionDecls : Std.HashMap Name Info := {}
deriving Inhabited

/-- Mutable state maintained during the SafeVerify check process. -/
structure State where
  /-- Outcomes of checking each target declaration -/
  checkOutcomes : Std.HashMap Name SafeVerifyOutcome := {}
  /-- Declarations that used disallowed axioms (name → axiom list) -/
  axiomViolations : Array (Name × Array Name) := #[]
deriving Inhabited

/-- The SafeVerify monad transformer: three-layer stack with Settings, Decls, and State -/
abbrev SafeVerifyT (m : Type → Type) := ReaderT Settings (ReaderT Decls (StateT State m))

/-- The SafeVerify monad: SafeVerifyT specialized to IO -/
abbrev SafeVerifyM := SafeVerifyT IO

/-- Get the Settings from the outer ReaderT layer -/
def getSettings {m : Type → Type} [Monad m] : SafeVerifyT m Settings := read

/-- Get the Decls from the middle ReaderT layer -/
def getDecls {m : Type → Type} [Monad m] : SafeVerifyT m Decls :=
  fun _ => read

end SafeVerify
