import { useRef, type ReactNode } from 'react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'

/**
 * Asks before something is taken away. The question is the title; the button that does it
 * names what it does, and the other keeps things as they are.
 *
 * It is opened from state, not from a trigger of its own, so when it closes it gives focus
 * back to whatever had it when it opened, the button that asked: Radix returns focus only
 * to a registered trigger, and would leave it nowhere otherwise.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  cancelLabel = 'Cancel',
  busy = false,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: ReactNode
  description?: ReactNode
  confirmLabel: ReactNode
  cancelLabel?: ReactNode
  busy?: boolean
  onConfirm: () => void
}) {
  const opener = useRef<HTMLElement | null>(null)
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent
        onOpenAutoFocus={() => {
          // Fired before focus moves into the dialog, so this is still the opener.
          opener.current = document.activeElement as HTMLElement | null
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault()
          const back = opener.current
          opener.current = null
          if (back && back.isConnected) back.focus()
        }}
      >
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description ? (
            <AlertDialogDescription>{description}</AlertDialogDescription>
          ) : (
            <AlertDialogDescription className="sr-only">
              This cannot be undone from the dashboard.
            </AlertDialogDescription>
          )}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>{cancelLabel}</AlertDialogCancel>
          <AlertDialogAction disabled={busy} onClick={onConfirm}>
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
