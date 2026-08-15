// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

import * as React from 'react'

import { cn } from '../../lib/utils'

function Separator({ className, ...props }: React.ComponentProps<'div'>) {
  return <div className={cn('h-px w-full bg-slate-200', className)} {...props} />
}

export { Separator }
