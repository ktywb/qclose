# Collect machine-readable timing data from an already routed Quartus snapshot.
#
# Positional arguments:
#   project revision output_dir paths_per_clock detailed_paths

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

proc json_number {value} {
    if {[string is double -strict $value] || [string is integer -strict $value]} {
        return $value
    }
    return "null"
}

proc json_object {fields} {
    return "\{[join $fields ,]\}"
}

proc json_array {items} {
    return "\[[join $items ,]\]"
}

proc string_field {name value} {
    return "[json_quote $name]:[json_quote $value]"
}

proc number_field {name value} {
    return "[json_quote $name]:[json_number $value]"
}

proc bool_field {name value} {
    if {$value} {
        set encoded true
    } else {
        set encoded false
    }
    return "[json_quote $name]:$encoded"
}

proc safe_path_info {path option} {
    if {[catch {get_path_info $path $option} value]} {
        return ""
    }
    return $value
}

proc safe_point_info {point option} {
    if {[catch {get_point_info $point $option} value]} {
        return ""
    }
    return $value
}

proc node_name {node_id point_type} {
    if {$node_id eq ""} {
        return ""
    }
    if {$point_type eq "re"} {
        return $node_id
    }
    if {[catch {get_node_info $node_id -name} name]} {
        return $node_id
    }
    return $name
}

proc clock_name {clock_id} {
    if {$clock_id eq ""} {
        return ""
    }
    if {[catch {get_clock_info -name $clock_id} name]} {
        return $clock_id
    }
    return $name
}

proc corner_name {corner_id} {
    if {$corner_id eq ""} {
        return ""
    }
    if {[catch {get_operating_conditions_info -display_name $corner_id} name]} {
        return $corner_id
    }
    return $name
}

proc path_json {path include_points clock_override} {
    set from_id [safe_path_info $path -from]
    set to_id [safe_path_info $path -to]
    set from_clock_id [safe_path_info $path -from_clock]
    set to_clock_id [safe_path_info $path -to_clock]
    set corner_id [safe_path_info $path -corner]

    set fields [list \
        [string_field type [safe_path_info $path -type]] \
        [number_field slack_ns [safe_path_info $path -slack]] \
        [number_field data_delay_ns [safe_path_info $path -data_delay]] \
        [number_field arrival_time_ns [safe_path_info $path -arrival_time]] \
        [number_field required_time_ns [safe_path_info $path -required_time]] \
        [number_field clock_skew_ns [safe_path_info $path -clock_skew]] \
        [number_field logic_levels [safe_path_info $path -num_logic_levels]] \
        [string_field from [node_name $from_id ""]] \
        [string_field to [node_name $to_id ""]] \
        [string_field from_clock [clock_name $from_clock_id]] \
        [string_field to_clock [clock_name $to_clock_id]] \
        [string_field requested_clock $clock_override] \
        [string_field corner [corner_name $corner_id]]]

    if {$include_points} {
        set points {}
        set point_index 0
        if {![catch {get_path_info $path -arrival_points} arrival_points]} {
            foreach_in_collection point $arrival_points {
                set point_type [safe_point_info $point -type]
                set node_id [safe_point_info $point -node]
                lappend points [json_object [list \
                    [number_field index $point_index] \
                    [string_field type $point_type] \
                    [string_field node [node_name $node_id $point_type]] \
                    [string_field edge [safe_point_info $point -edge]] \
                    [string_field rise_fall [safe_point_info $point -rise_fall]] \
                    [string_field location [safe_point_info $point -location]] \
                    [number_field incremental_delay_ns [safe_point_info $point -incremental_delay]] \
                    [number_field total_delay_ns [safe_point_info $point -total_delay]] \
                    [number_field fanout [safe_point_info $point -number_of_fanout]]]]
                incr point_index
            }
        }
        lappend fields "[json_quote points]:[json_array $points]"
    }
    return [json_object $fields]
}

proc record_warning {channel operation message} {
    set first_line [lindex [split $message "\n"] 0]
    puts $channel "$operation: $first_line"
    flush $channel
    post_message -type warning "Timing collector skipped $operation: $first_line"
}

if {$argc != 5} {
    fail "Usage: quartus_sta -t quartus_timing_collect.tcl <project> <revision> <output_dir> <paths_per_clock> <detailed_paths>"
}

lassign $argv project_name revision_name output_dir paths_per_clock detailed_path_count
if {![string is integer -strict $paths_per_clock] || $paths_per_clock < 1} {
    fail "paths_per_clock must be a positive integer"
}
if {![string is integer -strict $detailed_path_count] || $detailed_path_count < 0} {
    fail "detailed_paths must be a non-negative integer"
}

file mkdir $output_dir
set output_dir [file normalize $output_dir]
set warning_channel [open [file join $output_dir collector_warnings.txt] w]

project_open -revision $revision_name $project_name
create_timing_netlist -snapshot final
read_sdc
update_timing_netlist

set metadata_fields [list \
    [string_field project $project_name] \
    [string_field revision $revision_name] \
    [string_field quartus_version $::quartus(version)] \
    [string_field snapshot final] \
    [number_field paths_per_clock $paths_per_clock] \
    [number_field detailed_paths $detailed_path_count]]
set metadata_channel [open [file join $output_dir timing_metadata.json] w]
puts $metadata_channel [json_object $metadata_fields]
close $metadata_channel

set paths_channel [open [file join $output_dir paths.jsonl] w]
set clock_records {}
set total_paths 0

foreach_in_collection clock_id [get_clocks *] {
    set name [clock_name $clock_id]
    if {[catch {get_clock_info -period $clock_id} period]} {
        set period ""
    }
    if {[catch {
        get_timing_paths -setup -to_clock $clock_id -npaths $paths_per_clock -nworst 1 -detail full_path
    } clock_paths]} {
        record_warning $warning_channel "get_timing_paths for $name" $clock_paths
        set clock_records_for_domain 0
        set wns ""
    } else {
        set clock_records_for_domain 0
        set wns ""
        foreach_in_collection path $clock_paths {
            if {$clock_records_for_domain == 0} {
                set wns [safe_path_info $path -slack]
            }
            puts $paths_channel [path_json $path 0 $name]
            incr clock_records_for_domain
            incr total_paths
        }
    }
    lappend clock_records [json_object [list \
        [string_field name $name] \
        [number_field period_ns $period] \
        [number_field collected_setup_paths $clock_records_for_domain] \
        [number_field collected_wns_ns $wns]]]
}
close $paths_channel

set clocks_channel [open [file join $output_dir clocks.json] w]
puts $clocks_channel [json_array $clock_records]
close $clocks_channel

set detailed_channel [open [file join $output_dir detailed_paths.jsonl] w]
if {$detailed_path_count > 0} {
    if {[catch {
        get_timing_paths -setup -npaths $detailed_path_count -nworst 3 -detail full_path -show_routing
    } detailed_paths]} {
        record_warning $warning_channel get_detailed_timing_paths $detailed_paths
    } else {
        foreach_in_collection path $detailed_paths {
            puts $detailed_channel [path_json $path 1 ""]
        }
    }
}
close $detailed_channel

set report_paths [expr {max(500, $paths_per_clock)}]
set diagnostic_path_count [expr {max(1, $detailed_path_count)}]
if {[catch {
    report_timing_by_source_files -setup -npaths $report_paths -nworst 1 \
        -file [file join $output_dir timing_by_source_files.rpt]
} message]} {
    record_warning $warning_channel report_timing_by_source_files $message
}

if {[catch {
    get_timing_paths -setup -npaths $report_paths -nworst 3 -detail full_path
} bottleneck_paths]} {
    record_warning $warning_channel get_bottleneck_paths $bottleneck_paths
} else {
    if {[catch {
        puts "QTA_BOTTLENECK_BEGIN"
        report_bottleneck -stdout -nworst 50 -details $bottleneck_paths
        puts "QTA_BOTTLENECK_END"
    } message]} {
        record_warning $warning_channel report_bottleneck $message
    }
}

foreach {operation command} [list \
    check_timing [list check_timing -file [file join $output_dir check_timing.rpt]] \
    report_cdc_viewer [list report_cdc_viewer -summary -file [file join $output_dir cdc_summary.rpt]] \
    report_asynch_cdc [list report_asynch_cdc -detail summary -nentries 200 -file [file join $output_dir asynch_cdc_summary.rpt]] \
    report_logic_depth [list report_logic_depth -setup -detail histogram -npaths 500 -nworst 1 -file [file join $output_dir logic_depth.rpt]] \
    report_neighbor_paths [list report_neighbor_paths -setup -npaths $diagnostic_path_count -nworst 3 -neighbor_path_num 5 -extra_info all -file [file join $output_dir neighbor_paths.rpt]] \
    report_register_spread [list report_register_spread -num_registers 100 -min_sinks 10 -sink_type endpoint -spread_type tension -file [file join $output_dir register_spread.rpt]] \
    report_net_delay [list report_net_delay -nworst 100 -file [file join $output_dir net_delay.rpt]] \
    report_route_net_of_interest [list report_route_net_of_interest -num_nets 100 -file [file join $output_dir route_nets_of_interest.rpt]] \
    report_pipelining_info [list report_pipelining_info -max_rows 200 -file [file join $output_dir pipelining_info.rpt]] \
    report_retiming_restrictions [list report_retiming_restrictions -file [file join $output_dir retiming_restrictions.rpt]]] {
    if {[catch {uplevel #0 $command} message]} {
        record_warning $warning_channel $operation $message
    }
}

set summary_channel [open [file join $output_dir timing_collection_summary.json] w]
puts $summary_channel [json_object [list \
    [number_field clocks [llength $clock_records]] \
    [number_field paths $total_paths]]]
close $summary_channel

close $warning_channel
delete_timing_netlist
project_close
post_message "Timing collector wrote $total_paths path records to $output_dir"
