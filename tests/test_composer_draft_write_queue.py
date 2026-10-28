"""Runtime regression coverage for composer-draft write coalescing."""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for composer draft queue tests")


def _draft_runtime_source() -> str:
    start = SESSIONS_JS.index("// ── Composer draft persistence")
    end = SESSIONS_JS.index("const SESSION_VIEWED_COUNTS_KEY", start)
    return SESSIONS_JS[start:end]


def _run_node(scenario: str) -> dict:
    assert NODE is not None
    harness = textwrap.dedent(
        f"""
        const S={{session:{{session_id:'s1',composer_draft:{{}}}}}};
        const localStorage={{getItem:()=>null,setItem:()=>{{}},removeItem:()=>{{}}}};
        let calls=[];
        let active=0;
        let maxActive=0;
        let activeBySid={{}};
        let maxActiveBySid={{}};
        let resolvers=[];
        function api(path,opts){{
          const payload=JSON.parse(opts.body);
          calls.push(payload);
          active+=1;
          maxActive=Math.max(maxActive,active);
          activeBySid[payload.session_id]=(activeBySid[payload.session_id]||0)+1;
          maxActiveBySid[payload.session_id]=Math.max(maxActiveBySid[payload.session_id]||0,activeBySid[payload.session_id]);
          return new Promise((resolve,reject)=>resolvers.push((error)=>{{
            active-=1;
            activeBySid[payload.session_id]-=1;
            if(error) reject(new Error(error)); else resolve({{ok:true}});
          }}));
        }}
        {_draft_runtime_source()}
        {scenario}
        """
    )
    proc = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def test_same_session_immediate_writes_are_single_flight_and_latest_wins():
    result = _run_node(
        """
        (async()=>{
          const first=_saveComposerDraftNow('s1','A',[]);
          const second=_saveComposerDraftNow('s1','B',[]);
          const third=_saveComposerDraftNow('s1','C',[]);
          await Promise.resolve();
          const callsBeforeRelease=calls.map(x=>x.text);
          resolvers.shift()();
          await new Promise(resolve=>setImmediate(resolve));
          const callsAfterFirst=calls.map(x=>x.text);
          while(resolvers.length){
            resolvers.shift()();
            await new Promise(resolve=>setImmediate(resolve));
          }
          await Promise.all([first,second,third]);
          console.log(JSON.stringify({maxActive,callsBeforeRelease,callsAfterFirst,finalDraft:S.session.composer_draft.text}));
        })().catch(error=>{console.error(error);process.exit(1);});
        """
    )
    assert result == {
        "maxActive": 1,
        "callsBeforeRelease": ["A"],
        "callsAfterFirst": ["A", "C"],
        "finalDraft": "C",
    }


def test_clear_is_an_ordering_barrier_before_new_draft():
    result = _run_node(
        """
        (async()=>{
          const first=_saveComposerDraftNow('s1','A',[]);
          const clear=_clearComposerDraft('s1','A',[]);
          const newest=_saveComposerDraftNow('s1','C',[]);
          await Promise.resolve();
          while(resolvers.length){
            resolvers.shift()();
            await new Promise(resolve=>setImmediate(resolve));
          }
          await Promise.all([first,clear,newest]);
          console.log(JSON.stringify({maxActive,texts:calls.map(x=>x.text),finalDraft:S.session.composer_draft.text}));
        })().catch(error=>{console.error(error);process.exit(1);});
        """
    )
    assert result == {"maxActive": 1, "texts": ["A", "", "C"], "finalDraft": "C"}


def test_different_sessions_have_independent_single_flight_queues():
    result = _run_node(
        """
        (async()=>{
          const a1=_saveComposerDraftNow('s1','A1',[]);
          const a2=_saveComposerDraftNow('s1','A2',[]);
          const b1=_saveComposerDraftNow('s2','B1',[]);
          const b2=_saveComposerDraftNow('s2','B2',[]);
          await Promise.resolve();
          while(resolvers.length){
            resolvers.shift()();
            await new Promise(resolve=>setImmediate(resolve));
          }
          await Promise.all([a1,a2,b1,b2]);
          console.log(JSON.stringify({maxActive,maxActiveBySid,texts:calls.map(x=>`${x.session_id}:${x.text}`)}));
        })().catch(error=>{console.error(error);process.exit(1);});
        """
    )
    assert result["maxActive"] == 2
    assert result["maxActiveBySid"] == {"s1": 1, "s2": 1}
    assert result["texts"] == ["s1:A1", "s2:B1", "s1:A2", "s2:B2"]


def test_immediate_write_for_other_session_does_not_cancel_debounced_draft():
    result = _run_node(
        """
        (async()=>{
          _saveComposerDraft('s1','A',[]);
          const immediate=_saveComposerDraftNow('s2','B',[]);
          await Promise.resolve();
          resolvers.shift()();
          await immediate;
          await new Promise(resolve=>setTimeout(resolve,_DRAFT_SAVE_DELAY_MS+50));
          while(resolvers.length){
            resolvers.shift()();
            await new Promise(resolve=>setImmediate(resolve));
          }
          console.log(JSON.stringify({texts:calls.map(x=>`${x.session_id}:${x.text}`)}));
        })().catch(error=>{console.error(error);process.exit(1);});
        """
    )
    assert result == {"texts": ["s2:B", "s1:A"]}


def test_failed_write_does_not_stop_latest_write_or_mark_failure_persisted():
    result = _run_node(
        """
        (async()=>{
          const first=_saveComposerDraftNow('s1','A',[]);
          const newest=_saveComposerDraftNow('s1','C',[]);
          await Promise.resolve();
          resolvers.shift()('failed');
          await new Promise(resolve=>setImmediate(resolve));
          const draftAfterFailure=S.session.composer_draft.text||null;
          resolvers.shift()();
          await Promise.all([first,newest]);
          console.log(JSON.stringify({texts:calls.map(x=>x.text),draftAfterFailure,finalDraft:S.session.composer_draft.text}));
        })().catch(error=>{console.error(error);process.exit(1);});
        """
    )
    assert result == {"texts": ["A", "C"], "draftAfterFailure": None, "finalDraft": "C"}
