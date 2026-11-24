;;; pycontainer.el --- Project-local pycontainer commands -*- lexical-binding: t; -*-

(defvar pycontainer-python-executable "python"
  "Python interpreter with pycontainer-build installed.")

(defvar pycontainer-last-status nil
  "Exit status of the most recent pycontainer command.")

(defvar-local pycontainer-project-root nil
  "Explicit project root for this buffer.")

(defun pycontainer--run (dry-run)
  (setq pycontainer-last-status nil)
  (unless (and (stringp pycontainer-project-root)
               (> (length pycontainer-project-root) 0))
    (user-error "Set pycontainer-project-root for this buffer"))
  (let* ((root (expand-file-name pycontainer-project-root))
         (config (expand-file-name "pycontainer.toml" root)))
    (unless (file-directory-p root)
      (user-error "Project root is not a directory: %s" root))
    (unless (and (file-regular-p config) (file-readable-p config))
      (user-error "No readable pycontainer.toml in this buffer's project"))
    (let* ((default-directory (file-name-as-directory (expand-file-name root)))
           (context (directory-file-name default-directory))
           (output (get-buffer-create "*pycontainer*"))
           (arguments (append (list "-m" "pycontainer.cli" "build"
                                    "--context" context "--config" config)
                              (when dry-run (list "--dry-run")))))
      (with-current-buffer output
        (let ((inhibit-read-only t))
          (erase-buffer)))
      (setq pycontainer-last-status
            (apply #'call-process pycontainer-python-executable nil output t arguments))
      (display-buffer output)
      (unless (and (integerp pycontainer-last-status) (zerop pycontainer-last-status))
        (error "pycontainer exited with status %s; see *pycontainer*"
               pycontainer-last-status))
      (message "pycontainer completed"))))

(defun pycontainer-build ()
  "Build the project containing the current buffer."
  (interactive)
  (pycontainer--run nil))

(defun pycontainer-dry-run ()
  "Show the build plan for the project containing the current buffer."
  (interactive)
  (pycontainer--run t))

(provide 'pycontainer)
;;; pycontainer.el ends here
