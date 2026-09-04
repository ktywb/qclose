# Export selected Quartus compilation-report panels as JSON.
#
# Positional arguments:
#   project revision output_dir

package require Tcl 8.5

proc fail {message} {
    post_message -type error $message
    error $message
}

proc json_quote {value} {
    set escaped [string map [list \
        "\\" "\\\\" \
        "\"" "\\\"" \
        "\b" "\\b" \
        "\f" "\\f" \
        "\n" "\\n" \
        "\r" "\\r" \
        "\t" "\\t"] $value]
    return "\"${escaped}\""
}

proc slugify {value} {
    set result [string tolower $value]
    regsub -all {[^a-z0-9]+} $result {_} result
    return [string trim $result _]
}

proc panel_is_selected {panel_name} {
    set patterns {
        {Fitter Summary$}
        {Fitter Resource Usage Summary$}
        {Resource Usage Summary$}
        {Routing Usage Summary$}
        {Peak Wire Demand Summary$}
        {Peak Wire Demand Details$}
        {Non-Global High Fan-Out Signals$}
        {Nets with Highest Wire Count$}
        {Fitter Duplication Summary$}
        {Retiming Limit Summary$}
        {Fast Forward Summary}
        {Fmax Summary$}
        {Setup Summary$}
        {Hold Summary$}
        {Recovery Summary$}
        {Removal Summary$}
        {Clock Statistics$}
        {Clock Transfers.*Transfers$}
        {Unconstrained Paths.*Summary$}
        {Design Assistant}
    }
    foreach pattern $patterns {
        if {[regexp -- $pattern $panel_name]} {
            return 1
        }
    }
    return 0
}

if {$argc != 3} {
    fail "Usage: quartus_sh -t quartus_report_collect.tcl <project> <revision> <output_dir>"
}

lassign $argv project_name revision_name output_dir
file mkdir $output_dir
set output_dir [file normalize $output_dir]
set panels_dir [file join $output_dir panels]
file mkdir $panels_dir

project_open -revision $revision_name $project_name
load_package report
load_report

set panel_index_items {}
set selected_items {}
set selected_count 0

foreach panel_name [lsort -dictionary [get_report_panel_names]] {
    lappend panel_index_items [json_quote $panel_name]
    if {![panel_is_selected $panel_name]} {
        continue
    }

    set panel_id [get_report_panel_id $panel_name]
    if {$panel_id < 0} {
        continue
    }
    set slug [slugify $panel_name]
    if {$slug eq ""} {
        set slug panel
    }
    incr selected_count
    set filename [format "panel_%03d_%s.json" $selected_count [string range $slug 0 72]]

    if {[catch {get_report_panel_as_json -id $panel_id} panel_json]} {
        if {![string match "*Table is empty*" $panel_json]} {
            post_message -type warning "Cannot export report panel $panel_name: $panel_json"
        }
        continue
    }
    set panel_channel [open [file join $panels_dir $filename] w]
    puts $panel_channel $panel_json
    close $panel_channel

    lappend selected_items "\{\"name\":[json_quote $panel_name],\"file\":[json_quote panels/$filename],\"id\":$panel_id\}"
}

set index_channel [open [file join $output_dir panel_index.json] w]
puts $index_channel "\[[join $panel_index_items ,]\]"
close $index_channel

set selected_channel [open [file join $output_dir selected_panels.json] w]
puts $selected_channel "\[[join $selected_items ,]\]"
close $selected_channel

set report_type unknown
catch {set report_type [get_loaded_report_type]}
set metadata_channel [open [file join $output_dir report_metadata.json] w]
puts $metadata_channel "\{\"project\":[json_quote $project_name],\"revision\":[json_quote $revision_name],\"report_type\":[json_quote $report_type],\"panel_count\":[llength $panel_index_items],\"selected_panel_count\":[llength $selected_items]\}"
close $metadata_channel

unload_report
project_close
post_message "Report collector exported [llength $selected_items] of [llength $panel_index_items] panels to $output_dir"
