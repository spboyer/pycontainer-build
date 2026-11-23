if !exists('*job_start')
  echoerr 'pycontainer requires Vim with +job support'
  finish
endif

if !exists('g:pycontainer_python')
  let g:pycontainer_python = 'python'
endif
let g:pycontainer_last_status = -1

function! s:Project() abort
  let l:directory = get(b:, 'pycontainer_project_root', '')
  if empty(l:directory)
    throw 'pycontainer: set b:pycontainer_project_root for this buffer'
  endif
  let l:directory = fnamemodify(l:directory, ':p')
  if !isdirectory(l:directory)
    throw 'pycontainer: project root is not a directory: ' . l:directory
  endif
  let l:config = l:directory . '/pycontainer.toml'
  if !filereadable(l:config)
    throw 'pycontainer: no readable pycontainer.toml in project root: ' . l:config
  endif
  return [l:directory, l:config]
endfunction

function! s:Output(channel, message) abort
  echomsg a:message
endfunction

function! s:Run(dry_run) abort
  let g:pycontainer_last_status = -1
  let [l:directory, l:config] = s:Project()
  let l:args = [g:pycontainer_python, '-m', 'pycontainer.cli', 'build',
        \ '--context', l:directory, '--config', l:config]
  if a:dry_run
    call add(l:args, '--dry-run')
  endif
  let l:job = job_start(l:args, {'cwd': l:directory,
        \ 'out_cb': function('s:Output'), 'err_cb': function('s:Output')})
  if job_status(l:job) ==# 'fail'
    throw 'pycontainer: unable to start the configured Python interpreter'
  endif
  while job_status(l:job) ==# 'run'
    sleep 50m
  endwhile
  let g:pycontainer_last_status = job_info(l:job).exitval
  if g:pycontainer_last_status != 0
    throw printf('pycontainer: process exited with status %d', g:pycontainer_last_status)
  endif
endfunction

command! PycontainerBuild call <SID>Run(0)
command! PycontainerDryRun call <SID>Run(1)
